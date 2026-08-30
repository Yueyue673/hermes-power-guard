"""Power Guard shared runtime.

Coordinates every Hermes process for this profile through SQLite, then performs
one conservative, cancelable power action after all observed work is terminal.
The module is stdlib-only so the agent plugin and FastAPI plugin can both import
it without adding packages to Hermes' shared environment.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - older/standalone compatibility
    try:
        from hermes_cli.config import get_hermes_home
    except ImportError:  # pragma: no cover - standalone tests and source review
        def get_hermes_home() -> Path:
            configured = os.getenv("HERMES_HOME")
            return Path(configured).expanduser() if configured else Path.home() / ".hermes"

logger = logging.getLogger(__name__)

PLUGIN_ID = "power-guard"
STATE_DIR = get_hermes_home() / PLUGIN_ID
DB_PATH = STATE_DIR / "state.db"
LOG_PATH = STATE_DIR / "power-guard.log"
IS_WINDOWS = platform.system() == "Windows"
PROCESS_INSTANCE = f"{os.getpid()}:{time.time_ns()}:{uuid.uuid4().hex[:8]}"

DEFAULT_BLOCKER_KEYWORDS = [
    "需要你先",
    "请先登录",
    "需要你登录",
    "等待你确认",
    "需要人工",
    "无法继续",
    "不能继续",
    "需要你提供",
    "需要你授权",
    "需要你选择",
    "需要你操作",
    "need you to",
    "please log in",
    "waiting for your",
    "requires your approval",
    "cannot proceed",
    "can't proceed",
    "need your input",
    "manual action required",
]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "armed": False,
    "action": "sleep",  # shutdown | hibernate | sleep | lock | notify
    "trigger_mode": "done_or_blocked",  # done_only | done_or_blocked
    "include_failures": True,
    "include_interrupted": False,
    "detect_blocker_text": False,
    "blocker_keywords": DEFAULT_BLOCKER_KEYWORDS,
    "quiescence_seconds": 30,
    "countdown_seconds": 90,
    "require_user_idle": True,
    "user_idle_seconds": 300,
    "waiting_input_timeout_seconds": 900,
    "stale_heartbeat_seconds": 20,
    "stale_task_seconds": 3600,
    "arm_expiry_minutes": 720,
    "background_process_policy": "wait_new",  # wait_new | wait_all | ignore_detached
    "cancel_on_new_activity": True,
    "prevent_sleep_while_working": True,
    "turn_off_display_when_idle": False,
    "display_off_idle_seconds": 300,
    "lower_hermes_priority": False,
    "power_plan": "unchanged",  # unchanged | balanced | power_saver
    "restore_power_plan": True,
    "protected_processes": [],
    "dry_run": False,
}

ALLOWED_ACTIONS = {"shutdown", "hibernate", "sleep", "lock", "notify"}
ALLOWED_TRIGGER_MODES = {"done_only", "done_or_blocked"}
ALLOWED_BACKGROUND_POLICIES = {"wait_new", "wait_all", "ignore_detached"}
ALLOWED_POWER_PLANS = {"unchanged", "balanced", "power_saver"}

# A turn ending for one of these reasons cannot make further automatic progress
# inside the same run_conversation call. The user may choose whether that counts
# as a terminal blocker through trigger_mode/include_failures.
BLOCKED_REASON_PREFIXES = (
    "guardrail_halt",
    "budget_exhausted",
    "max_iterations_reached",
    "error_near_max_iterations",
    "pending_tool_result",
    "empty_response_exhausted",
    "all_retries_exhausted_no_response",
    "partial_stream_recovery",
    "fallback_prior_turn_content",
    "interrupted_during_api_call",
    "ollama_runtime_context_too_small",
    "session_persistence_failed",
)

POWER_PLAN_GUIDS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}

# The renderer sends an authenticated heartbeat through plugin_api.py. Losing
# the Desktop UI (plugin disabled, app closed, route disconnected) is a hard
# cancel condition: no visible countdown/cancel affordance means no power
# action. Keep the grace long enough for Chromium background timer throttling.
UI_HEARTBEAT_TIMEOUT_SECONDS = 45

_MONITOR_LOCK = threading.Lock()
_MONITOR_STARTED = False
_MONITOR_STOP = threading.Event()
_LAST_MONITOR_WALL: Optional[float] = None
_LAST_MONITOR_MONOTONIC: Optional[float] = None
_LOCAL_POWER_LOCK = threading.Lock()
_EXECUTION_STATE_ACTIVE = False
_ORIGINAL_PRIORITY_CLASS: Optional[int] = None
_DISPLAY_TURNED_OFF_FOR_IDLE_WINDOW = False


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _log_event(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        logger.debug("Power Guard file logging failed", exc_info=True)


def _deepcopy_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(settings, ensure_ascii=False))


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def validate_settings(raw: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = _deepcopy_settings(base or DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return settings

    for key in (
        "armed",
        "include_failures",
        "include_interrupted",
        "detect_blocker_text",
        "require_user_idle",
        "cancel_on_new_activity",
        "prevent_sleep_while_working",
        "turn_off_display_when_idle",
        "lower_hermes_priority",
        "restore_power_plan",
        "dry_run",
    ):
        if key in raw:
            settings[key] = bool(raw[key])

    action = str(raw.get("action", settings["action"]))
    if action in ALLOWED_ACTIONS:
        settings["action"] = action

    mode = str(raw.get("trigger_mode", settings["trigger_mode"]))
    if mode in ALLOWED_TRIGGER_MODES:
        settings["trigger_mode"] = mode

    policy = str(raw.get("background_process_policy", settings["background_process_policy"]))
    if policy in ALLOWED_BACKGROUND_POLICIES:
        settings["background_process_policy"] = policy

    power_plan = str(raw.get("power_plan", settings["power_plan"]))
    if power_plan in ALLOWED_POWER_PLANS:
        settings["power_plan"] = power_plan

    limits = {
        "quiescence_seconds": (30, 3600),
        "countdown_seconds": (60, 3600),
        "user_idle_seconds": (0, 86400),
        "waiting_input_timeout_seconds": (60, 86400),
        "stale_heartbeat_seconds": (8, 300),
        "stale_task_seconds": (300, 604800),
        "arm_expiry_minutes": (30, 10080),
        "display_off_idle_seconds": (30, 86400),
    }
    for key, (minimum, maximum) in limits.items():
        if key in raw:
            settings[key] = _coerce_int(raw[key], settings[key], minimum, maximum)

    if "blocker_keywords" in raw:
        values = raw["blocker_keywords"]
        if isinstance(values, str):
            values = [line.strip() for line in re.split(r"[\n,]", values) if line.strip()]
        if isinstance(values, list):
            settings["blocker_keywords"] = [str(item).strip()[:160] for item in values if str(item).strip()][:100]

    if "protected_processes" in raw:
        values = raw["protected_processes"]
        if isinstance(values, str):
            values = [item.strip() for item in re.split(r"[\n,;]", values) if item.strip()]
        if isinstance(values, list):
            settings["protected_processes"] = [
                Path(str(item).strip()).name.lower()[:128]
                for item in values
                if str(item).strip()
            ][:100]

    return settings


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            profile TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            owner_instance TEXT NOT NULL,
            owner_pid INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            last_tool TEXT NOT NULL DEFAULT '',
            response_blocker TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            ended_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_power_guard_tasks_generation
            ON tasks(generation, status, updated_at);
        CREATE TABLE IF NOT EXISTS heartbeats (
            owner_instance TEXT PRIMARY KEY,
            owner_pid INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            active_processes INTEGER NOT NULL DEFAULT 0,
            active_delegations INTEGER NOT NULL DEFAULT 0,
            process_details TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS ui_heartbeats (
            instance_id TEXT PRIMARY KEY,
            updated_at REAL NOT NULL,
            busy_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    stored_settings = _meta_get(conn, "settings")
    if stored_settings is None:
        _meta_set(conn, "settings", DEFAULT_SETTINGS)
        _meta_set(conn, "migration_background_policy_041", True)
    elif _meta_get(conn, "migration_background_policy_041") is None:
        # 0.4.0 did not record whether wait_all came from a preset or an
        # explicit safety choice. Preserve it rather than silently weakening a
        # user's strict policy; the Desktop UI exposes wait_new directly.
        _meta_set(conn, "migration_background_policy_041", True)
    if _meta_get(conn, "runtime") is None:
        _meta_set(conn, "runtime", _fresh_runtime())


def _fresh_runtime() -> Dict[str, Any]:
    return {
        "generation": 0,
        "state": "disarmed",
        "armed_at": None,
        "armed_expires_at": None,
        "arm_request_token": "",
        "arm_request_started_at": None,
        "scope_mode": "next",
        "scope_session_id": "",
        "scope_session_ids": [],
        "captured_task_count": 0,
        "background_baseline": {},
        "activity_seen": False,
        "snooze_until": None,
        "quiet_since": None,
        "countdown_due": None,
        "countdown_last_input_at": None,
        "countdown_reason": "",
        "countdown_token": "",
        "test_only": False,
        "action_claimed_by": "",
        "last_action": None,
        "last_error": "",
        "blocked_terminal_count": 0,
        "completed_terminal_count": 0,
        "power_plan_previous": "",
        "power_plan_applied": "",
        "ui_heartbeat_at": None,
        "desktop_busy_count": 0,
        "updated_at": _now(),
    }


def _meta_get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, _json(value), _now()),
    )


def _event(conn: sqlite3.Connection, kind: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO events(created_at,kind,message,details) VALUES(?,?,?,?)",
        (_now(), kind[:80], message[:1000], _json(details or {})),
    )
    conn.execute(
        "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 300)"
    )
    _log_event(f"{kind}: {message}")


def get_settings() -> Dict[str, Any]:
    with _connect() as conn:
        stored = _meta_get(conn, "settings") or {}
    return validate_settings(stored)


def initialize_desktop_backend() -> None:
    """Fail closed across Desktop backend restarts.

    Preferences persist; an armed campaign, countdown, or half-claimed action
    does not. Replaying a destructive operation after a crash is worse than
    requiring one fresh click from the user.
    """
    if os.getenv("HERMES_DESKTOP") != "1":
        return
    pending_plan_recovery: Optional[tuple[str, str]] = None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if runtime.get("power_plan_applied") == "pending":
            pending_plan_recovery = (
                str(runtime.get("power_plan_previous") or ""),
                str(settings.get("power_plan") or "unchanged"),
            )
        # Renderer leases belong to the previous backend lifetime.
        conn.execute("DELETE FROM ui_heartbeats")
        runtime["ui_heartbeat_at"] = None
        runtime["desktop_busy_count"] = 0
        stale_state = str(runtime.get("state") or "")
        if settings.get("armed") or stale_state in {
            "armed_waiting_for_task",
            "running",
            "armed_waiting_for_quiet",
            "waiting_for_terminal_signal",
            "waiting_for_desktop_ui",
            "terminal_not_eligible",
            "waiting_for_protected_process",
            "waiting_for_idle_check",
            "waiting_for_user_idle",
            "quiescence",
            "countdown",
            "executing",
        }:
            settings["armed"] = False
            runtime["state"] = "disarmed"
            runtime["armed_expires_at"] = None
            runtime["snooze_until"] = None
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["test_only"] = False
            runtime["action_claimed_by"] = ""
            runtime["ui_heartbeat_at"] = None
            runtime["updated_at"] = _now()
            if stale_state == "executing":
                runtime["last_error"] = "Desktop backend restarted during action claim; action was not retried"
            _meta_set(conn, "settings", settings)
            _meta_set(conn, "runtime", runtime)
            _event(conn, "restart_disarm", f"Desktop backend start disarmed stale state: {stale_state}")
        _meta_set(conn, "runtime", runtime)
        conn.commit()
    if pending_plan_recovery:
        _recover_pending_power_plan(*pending_plan_recovery)


def update_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    should_restore = False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = validate_settings(_meta_get(conn, "settings") or {})
        updated = validate_settings(patch, base=current)
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        policy_changed = any(
            updated.get(key) != current.get(key)
            for key in DEFAULT_SETTINGS
            if key != "armed"
        )
        if runtime.get("arm_request_token"):
            runtime["arm_request_token"] = ""
            runtime["arm_request_started_at"] = None
            runtime["updated_at"] = _now()
            _meta_set(conn, "runtime", runtime)
            _event(conn, "arm_cancelled", "Settings changed while confirmation was being prepared")
        claim_in_flight = runtime.get("state") == "executing"
        if policy_changed and (current.get("armed") or claim_in_flight):
            # A confirmed campaign is a snapshot of the exact policy the user
            # reviewed. This remains true after the claim atomically disarms
            # settings: edits must still cancel the in-flight OS request.
            updated["armed"] = False
            should_restore = bool(
                current.get("restore_power_plan") and runtime.get("power_plan_previous")
            )
            runtime["state"] = "disarmed"
            runtime["armed_expires_at"] = None
            runtime["snooze_until"] = None
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = _now()
            _meta_set(conn, "runtime", runtime)
            _event(
                conn,
                "action_aborted" if claim_in_flight else "settings_disarm",
                "Settings changed; confirmation is required again",
            )
        else:
            updated["armed"] = current.get("armed", False)
        _meta_set(conn, "settings", updated)
        _event(conn, "settings", "Settings updated")
        conn.commit()
    if should_restore:
        _manage_global_power_plan(updated, working=False)
    return updated


def _task_key(session_id: str, task_id: str, turn_id: str, profile: str = "") -> str:
    # turn_id is only locally unique on some Hermes surfaces. Hash the full,
    # untouched identity tuple so parallel sessions and profiles cannot collide;
    # the original identifiers remain available in their dedicated DB columns.
    raw = json.dumps(
        [str(profile or "default"), str(session_id), str(task_id), str(turn_id)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    identity = str(turn_id or task_id or session_id or "unknown")[:80]
    return f"turn:{digest}:{identity}"


def _active_scope_task_ids(session_id: str, profile: str = "") -> list[str]:
    if not session_id:
        return []
    with _connect() as conn:
        if profile:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM tasks WHERE session_id=? AND profile=? "
                "AND status IN ('running','waiting_input') AND task_id<>''",
                (session_id, profile),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM tasks WHERE session_id=? "
                "AND status IN ('running','waiting_input') AND task_id<>''",
                (session_id,),
            ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def _scope_process_keys(task_ids: list[str]) -> set[str]:
    if not task_ids:
        return set()
    try:
        from tools.process_registry import process_registry

        keys: set[str] = set()
        for task_id in task_ids:
            for row in process_registry.list_sessions(task_id=task_id):
                if row.get("status") == "running" and row.get("session_id"):
                    keys.add(f"process:{row['session_id']}")
        return keys
    except Exception:
        logger.exception("Power Guard could not identify current-project processes")
        return set()


def arm(
    overrides: Optional[Dict[str, Any]] = None,
    *,
    current_session_id: str = "",
    current_session_ids: Optional[list[str]] = None,
    current_profile: str = "",
) -> Dict[str, Any]:
    request_token = uuid.uuid4().hex
    request_started_at = _now()
    current_session_id = str(current_session_id or "")[:240]
    scope_session_ids = []
    for value in [current_session_id, *(current_session_ids or [])]:
        normalized = str(value or "")[:240]
        if normalized and normalized not in scope_session_ids:
            scope_session_ids.append(normalized)
    current_session_id = scope_session_ids[0] if scope_session_ids else ""
    current_profile = str(current_profile or "")[:120]
    arm_cancelled = False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings_snapshot = validate_settings(_meta_get(conn, "settings") or {})
        if overrides:
            settings_snapshot = validate_settings(overrides, base=settings_snapshot)
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        runtime["arm_request_token"] = request_token
        runtime["arm_request_started_at"] = request_started_at
        runtime["updated_at"] = request_started_at
        _meta_set(conn, "runtime", runtime)
        _event(conn, "arm_preparing", "Sampling pre-existing background work")
        conn.commit()

    # Sample outside the write transaction. The effective arm boundary is the
    # moment after this snapshot: anything observed here is pre-arm; anything
    # starting afterwards must block. Holding BEGIN IMMEDIATE while calling
    # live registries can stall cancellation and UI heartbeats.
    observed_details: list[Dict[str, Any]] = []
    if settings_snapshot.get("background_process_policy") == "wait_new":
        sample_now = _now()
        cutoff = sample_now - int(settings_snapshot.get("stale_heartbeat_seconds") or 20)
        with _connect() as conn:
            rows = conn.execute(
                "SELECT process_details FROM heartbeats WHERE updated_at>=?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            try:
                row_details = json.loads(row["process_details"] or "[]")
            except (TypeError, ValueError):
                row_details = []
            observed_details.extend(item for item in row_details if isinstance(item, dict))
        _, _, local_details = _background_snapshot(
            settings_snapshot,
            include_preexisting=True,
        )
        observed_details.extend(local_details)
    scope_task_ids = sorted({
        task_id
        for session_id in scope_session_ids
        for task_id in _active_scope_task_ids(session_id, current_profile)
    })
    scope_process_keys = _scope_process_keys(scope_task_ids)
    armed_at = _now()
    baseline: Dict[str, int] = {}
    for item in observed_details:
        if _background_detail_key(item).split(":", 1)[0] != "process":
            continue
        started_at = item.get("started_at")
        if started_at is not None:
            try:
                if float(started_at) > armed_at:
                    continue
            except (TypeError, ValueError):
                pass
        key = _background_detail_key(item)
        if key in scope_process_keys:
            # Existing terminals launched by the current project belong to the
            # captured cohort; unlike unrelated old daemons, they must finish.
            continue
        baseline[key] = max(baseline.get(key, 0), max(1, int(item.get("units") or 1)))

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        if overrides:
            settings = validate_settings(overrides, base=settings)
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if str(runtime.get("arm_request_token") or "") != request_token:
            arm_cancelled = True
        else:
            ui_heartbeat_at = runtime.get("ui_heartbeat_at")
            generation = int(runtime.get("generation") or 0) + 1
            if scope_session_ids:
                placeholders = ",".join("?" for _ in scope_session_ids)
                profile_clause = " AND profile=?" if current_profile else ""
                params: list[Any] = [generation, request_started_at, *scope_session_ids]
                if current_profile:
                    params.append(current_profile)
                conn.execute(
                    "UPDATE tasks SET generation=? WHERE updated_at>=? OR "
                    f"(session_id IN ({placeholders}){profile_clause} "
                    "AND status IN ('running','waiting_input'))",
                    params,
                )
            else:
                conn.execute(
                    "UPDATE tasks SET generation=? WHERE updated_at>=?",
                    (generation, request_started_at),
                )
            activity_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE generation=?",
                    (generation,),
                ).fetchone()[0]
            )
            settings["armed"] = True
            runtime = _fresh_runtime()
            runtime.update(
                {
                    "generation": generation,
                    "state": "running" if activity_count else "armed_waiting_for_task",
                    "armed_at": armed_at,
                    "armed_expires_at": armed_at + int(settings["arm_expiry_minutes"]) * 60,
                    "scope_mode": "current" if activity_count and current_session_id else "next",
                    "scope_session_id": current_session_id if activity_count else "",
                    "scope_session_ids": scope_session_ids if activity_count else [],
                    "captured_task_count": activity_count,
                    "background_baseline": baseline if settings.get("background_process_policy") == "wait_new" else {},
                    "activity_seen": bool(activity_count),
                    "ui_heartbeat_at": ui_heartbeat_at,
                }
            )
            _meta_set(conn, "settings", settings)
            _meta_set(conn, "runtime", runtime)
            _event(
                conn,
                "armed",
                f"Armed generation {generation}",
                {
                    "action": settings["action"],
                    "background_baseline_count": len(baseline),
                    "boundary_task_count": activity_count,
                    "scope_mode": "current" if activity_count and current_session_id else "next",
                    "scope_session_id": current_session_id if activity_count else "",
                    "scope_session_ids": scope_session_ids if activity_count else [],
                },
            )
        conn.commit()
    if arm_cancelled:
        raise RuntimeError("Automatic sleep confirmation was cancelled by a newer action")
    start_monitor()
    return get_status()


def cancel(reason: str = "user_cancelled") -> Dict[str, Any]:
    should_restore = False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        previous_plan = str(runtime.get("power_plan_previous") or "")
        should_restore = bool(settings.get("restore_power_plan") and previous_plan)
        settings["armed"] = False
        runtime.update(
            {
                "state": "disarmed",
                "armed_expires_at": None,
                "arm_request_token": "",
                "arm_request_started_at": None,
                "snooze_until": None,
                "quiet_since": None,
                "countdown_due": None,
                "countdown_reason": "",
                "countdown_token": "",
                "test_only": False,
                "action_claimed_by": "",
                "last_error": "",
                "updated_at": _now(),
            }
        )
        _meta_set(conn, "settings", settings)
        _meta_set(conn, "runtime", runtime)
        _event(conn, "cancelled", f"Cancelled: {reason}")
        conn.commit()
    if should_restore:
        _manage_global_power_plan(settings, working=False)
    return get_status()


def snooze(minutes: int = 15) -> Dict[str, Any]:
    minutes = _coerce_int(minutes, 15, 5, 240)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if not settings.get("armed"):
            conn.rollback()
            raise RuntimeError("Power Guard is not armed")
        if runtime.get("state") == "executing":
            conn.rollback()
            raise RuntimeError("The power action has already entered its execution phase")
        runtime["snooze_until"] = _now() + minutes * 60
        runtime["state"] = "snoozed"
        runtime["quiet_since"] = None
        runtime["countdown_due"] = None
        runtime["countdown_reason"] = ""
        runtime["countdown_token"] = ""
        runtime["action_claimed_by"] = ""
        runtime["updated_at"] = _now()
        _meta_set(conn, "runtime", runtime)
        _event(conn, "snoozed", f"Power action delayed by {minutes} minute(s)")
        conn.commit()
    return get_status()


def resume_snooze() -> Dict[str, Any]:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if not settings.get("armed"):
            conn.rollback()
            raise RuntimeError("Power Guard is not armed")
        runtime["snooze_until"] = None
        runtime["state"] = "armed_waiting_for_quiet"
        runtime["quiet_since"] = None
        runtime["updated_at"] = _now()
        _meta_set(conn, "runtime", runtime)
        _event(conn, "snooze_resumed", "Delay cleared; eligibility will be recomputed")
        conn.commit()
    return get_status()


def start_test_countdown(seconds: int = 8) -> Dict[str, Any]:
    seconds = _coerce_int(seconds, 8, 3, 60)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        if settings.get("armed"):
            conn.rollback()
            raise RuntimeError("Cancel the armed campaign before starting a simulation test")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        runtime.update(
            {
                "state": "countdown",
                "countdown_due": _now() + seconds,
                "countdown_reason": "simulation_test",
                "countdown_token": uuid.uuid4().hex,
                "test_only": True,
                "action_claimed_by": "",
                "last_error": "",
                "updated_at": _now(),
            }
        )
        _meta_set(conn, "runtime", runtime)
        _event(conn, "test", f"Started {seconds}s simulated countdown")
        conn.commit()
    start_monitor()
    return get_status()


def record_ui_heartbeat(
    busy_count: Optional[int] = None,
    instance_id: str = "desktop-status",
) -> None:
    """Lease the visible cancel affordance without last-writer-wins races."""
    now = _now()
    normalized_id = str(instance_id or "desktop-status")[:160]
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT busy_count FROM ui_heartbeats WHERE instance_id=?",
            (normalized_id,),
        ).fetchone()
        normalized_busy = (
            max(0, min(1000, int(busy_count)))
            if busy_count is not None
            else int(previous["busy_count"] if previous else 0)
        )
        conn.execute(
            "INSERT INTO ui_heartbeats(instance_id,updated_at,busy_count) VALUES(?,?,?) "
            "ON CONFLICT(instance_id) DO UPDATE SET "
            "updated_at=excluded.updated_at,busy_count=excluded.busy_count",
            (normalized_id, now, normalized_busy),
        )
        conn.execute(
            "DELETE FROM ui_heartbeats WHERE updated_at<?",
            (now - UI_HEARTBEAT_TIMEOUT_SECONDS * 4,),
        )
        aggregate = conn.execute(
            "SELECT MAX(updated_at), COALESCE(MAX(busy_count),0) FROM ui_heartbeats "
            "WHERE updated_at>=?",
            (now - UI_HEARTBEAT_TIMEOUT_SECONDS,),
        ).fetchone()
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        runtime["ui_heartbeat_at"] = float(aggregate[0]) if aggregate[0] is not None else None
        runtime["desktop_busy_count"] = int(aggregate[1] or 0)
        runtime["updated_at"] = now
        _meta_set(conn, "runtime", runtime)
        conn.commit()


def record_external_activity(reason: str = "external_user_activity") -> None:
    """Cancel a stale quiet/countdown snapshot before a queued turn starts."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        state = runtime.get("state")
        if state == "executing" or (
            settings.get("armed") and state in {"quiescence", "countdown"}
        ):
            runtime["state"] = "disarmed" if state == "executing" else "armed_waiting_for_quiet"
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = _now()
            _meta_set(conn, "runtime", runtime)
            _event(
                conn,
                "action_aborted" if state == "executing" else "countdown_cancelled",
                f"New external activity: {reason}",
            )
        conn.commit()


def record_turn_start(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    platform_name: str = "",
    profile: str = "",
) -> None:
    key = _task_key(session_id, task_id, turn_id, profile)
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        generation = int(runtime.get("generation") or 0)
        conn.execute(
            """
            INSERT INTO tasks(task_key,session_id,task_id,turn_id,profile,platform,
                owner_instance,owner_pid,generation,status,started_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'running',?,?)
            ON CONFLICT(task_key) DO UPDATE SET
                session_id=excluded.session_id,
                task_id=excluded.task_id,
                turn_id=excluded.turn_id,
                profile=excluded.profile,
                platform=excluded.platform,
                owner_instance=excluded.owner_instance,
                owner_pid=excluded.owner_pid,
                generation=excluded.generation,
                status='running', outcome='', reason='', ended_at=NULL,
                updated_at=excluded.updated_at
            """,
            (
                key,
                session_id,
                task_id,
                turn_id,
                profile,
                platform_name,
                PROCESS_INSTANCE,
                os.getpid(),
                generation,
                now,
                now,
            ),
        )
        if runtime.get("state") == "executing" and runtime.get("countdown_token"):
            runtime["state"] = "disarmed"
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = now
            _meta_set(conn, "runtime", runtime)
            _event(conn, "action_aborted", "New Hermes turn arrived before the power call")
        elif settings.get("armed"):
            runtime["activity_seen"] = True
            runtime["quiet_since"] = None
            if runtime.get("state") == "countdown" and runtime.get("test_only"):
                runtime["state"] = "armed_waiting_for_quiet"
                runtime["countdown_due"] = None
                runtime["countdown_token"] = ""
                runtime["test_only"] = False
                _event(conn, "countdown_cancelled", "Simulated countdown cancelled by new task")
            elif runtime.get("state") == "countdown" and settings.get("cancel_on_new_activity", True):
                runtime["state"] = "running"
                runtime["countdown_due"] = None
                runtime["countdown_reason"] = ""
                runtime["countdown_token"] = ""
                runtime["action_claimed_by"] = ""
                _event(conn, "countdown_cancelled", "Countdown cancelled by new Hermes activity")
            else:
                runtime["state"] = "running"
            runtime["updated_at"] = now
            _meta_set(conn, "runtime", runtime)
        conn.commit()
    start_monitor()


def record_tool_state(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    profile: str = "",
    tool_name: str = "",
    waiting: bool = False,
) -> None:
    key = _task_key(session_id, task_id, turn_id, profile)
    status = "waiting_input" if waiting else "running"
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, last_tool=?, updated_at=? "
            "WHERE task_key=? AND status IN ('running','waiting_input')",
            (status, tool_name[:160], _now(), key),
        )


def record_latest_owner_waiting(reason: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT task_key FROM tasks WHERE owner_instance=? AND status='running' "
            "ORDER BY updated_at DESC LIMIT 1",
            (PROCESS_INSTANCE,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tasks SET status='waiting_input', reason=?, updated_at=? WHERE task_key=?",
                (reason[:500], _now(), row["task_key"]),
            )


def record_latest_owner_resumed(choice: str = "") -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT task_key FROM tasks WHERE owner_instance=? AND "
            "(status='waiting_input' OR (status='blocked' AND reason='waiting_for_user_timeout')) "
            "ORDER BY updated_at DESC LIMIT 1",
            (PROCESS_INSTANCE,),
        ).fetchone()
        if row:
            if choice in {"deny", "timeout", "smart_deny"}:
                conn.execute(
                    "UPDATE tasks SET status='blocked', outcome='blocked', reason=?, ended_at=?, updated_at=? "
                    "WHERE task_key=?",
                    (f"approval_{choice}", _now(), _now(), row["task_key"]),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status='running', outcome='', reason='', ended_at=NULL, updated_at=? "
                    "WHERE task_key=?",
                    (_now(), row["task_key"]),
                )


def _find_blocker(response: str, settings: Dict[str, Any]) -> str:
    if not settings.get("detect_blocker_text") or not response:
        return ""
    folded = response.casefold()
    for keyword in settings.get("blocker_keywords") or []:
        needle = str(keyword).strip()
        if needle and needle.casefold() in folded:
            return needle[:160]
    return ""


def record_response(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    profile: str = "",
    response: str = "",
) -> None:
    key = _task_key(session_id, task_id, turn_id, profile)
    settings = get_settings()
    blocker = _find_blocker(response, settings)
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET response_blocker=?, updated_at=? WHERE task_key=?",
            (blocker, _now(), key),
        )


def _reason_is_blocked(reason: str) -> bool:
    value = str(reason or "")
    return any(
        value == prefix or value.startswith(prefix + "(") or value.startswith(prefix + ":")
        for prefix in BLOCKED_REASON_PREFIXES
    )


def finish_turn(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    profile: str = "",
    completed: bool = False,
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: str = "",
) -> None:
    key = _task_key(session_id, task_id, turn_id, profile)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT response_blocker FROM tasks WHERE task_key=?", (key,)).fetchone()
        blocker = str(row["response_blocker"] if row else "")
        reason = str(turn_exit_reason or "")
        if blocker:
            status, outcome, final_reason = "blocked", "blocked", f"response:{blocker}"
        elif interrupted:
            status, outcome, final_reason = "interrupted", "interrupted", reason or "interrupted"
        elif failed:
            status, outcome, final_reason = "failed", "failed", reason or "failed"
        elif _reason_is_blocked(reason):
            status, outcome, final_reason = "blocked", "blocked", reason
        elif reason.startswith("text_response(") or (completed and not reason):
            status, outcome, final_reason = "completed", "completed", reason or "completed"
        else:
            status, outcome, final_reason = "unknown", "unknown", reason or "unclassified_terminal"

        now = _now()
        if row is None:
            runtime = _meta_get(conn, "runtime") or _fresh_runtime()
            conn.execute(
                """
                INSERT INTO tasks(task_key,session_id,task_id,turn_id,profile,owner_instance,
                    owner_pid,generation,status,outcome,reason,started_at,updated_at,ended_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    session_id,
                    task_id,
                    turn_id,
                    profile,
                    PROCESS_INSTANCE,
                    os.getpid(),
                    int(runtime.get("generation") or 0),
                    status,
                    outcome,
                    final_reason,
                    now,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status=?, outcome=?, reason=?, ended_at=?, updated_at=? WHERE task_key=?",
                (status, outcome, final_reason[:500], now, now, key),
            )
        _event(conn, "turn_terminal", f"{key} -> {status}", {"reason": final_reason})
        conn.commit()
    start_monitor()


def record_kanban(task_id: str, status: str, reason: str = "", board: str = "", profile: str = "") -> None:
    if not task_id:
        return
    key = f"kanban:{profile or 'default'}:{board or 'default'}:{task_id}"
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        generation = int(runtime.get("generation") or 0)
        outcome = status if status in {"completed", "blocked", "failed"} else ""
        ended = now if outcome else None
        conn.execute(
            """
            INSERT INTO tasks(task_key,task_id,profile,platform,owner_instance,owner_pid,
                generation,status,outcome,reason,started_at,updated_at,ended_at)
            VALUES(?,?,?,'kanban',?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_key) DO UPDATE SET
                profile=excluded.profile,
                owner_instance=excluded.owner_instance,
                owner_pid=excluded.owner_pid,
                generation=excluded.generation,
                status=excluded.status,
                outcome=excluded.outcome,
                reason=excluded.reason,
                started_at=CASE
                    WHEN tasks.generation<>excluded.generation THEN excluded.started_at
                    ELSE tasks.started_at
                END,
                updated_at=excluded.updated_at,
                ended_at=excluded.ended_at
            """,
            (
                key,
                task_id,
                profile,
                PROCESS_INSTANCE,
                os.getpid(),
                generation,
                status,
                outcome,
                reason[:500],
                now,
                now,
                ended,
            ),
        )
        settings = validate_settings(_meta_get(conn, "settings") or {})
        if settings.get("armed") and status == "running":
            runtime["activity_seen"] = True
            runtime["state"] = "running"
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_token"] = ""
            _meta_set(conn, "runtime", runtime)
        conn.commit()
    start_monitor()


def _background_detail_key(detail: Dict[str, Any]) -> str:
    source = str(detail.get("source") or "")
    identifier = str(detail.get("id") or "")
    if not source:
        command = str(detail.get("command") or "")
        if identifier.startswith("sensor:async"):
            source = "sensor_delegation"
        elif identifier.startswith("sensor:cron"):
            source = "sensor_cron"
        elif identifier.startswith("sensor:"):
            source = "sensor_process"
        elif command == "cron job":
            source = "cron"
        else:
            source = "process"
    return f"{source}:{identifier}"


def _background_detail_bucket(detail: Dict[str, Any]) -> str:
    source = _background_detail_key(detail).split(":", 1)[0]
    if source in {"delegation", "completion_queue", "sensor_delegation"}:
        return "delegation"
    return "process"


def _process_started_at(row: Dict[str, Any], now: float) -> float:
    raw = row.get("started_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return float(time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%S")))
        except (TypeError, ValueError, OverflowError):
            pass
    uptime = max(0.0, float(row.get("uptime_seconds") or 0.0))
    return now - uptime


def _background_snapshot(
    settings: Dict[str, Any],
    *,
    baseline_ids: Optional[set[str]] = None,
    armed_at: Optional[float] = None,
    include_preexisting: bool = False,
) -> tuple[int, int, list[Dict[str, Any]]]:
    """Return background work visible to this process.

    ``wait_new`` is campaign-scoped: work already running when the user
    confirms the one-shot action is a baseline, not a permanent veto. Work
    that starts afterwards still blocks. Sensor failures are never filtered.
    """
    policy = str(settings.get("background_process_policy") or "wait_new")
    baseline = baseline_ids or set()
    details: list[Dict[str, Any]] = []
    now = _now()

    def add(detail: Dict[str, Any]) -> None:
        detail = dict(detail)
        detail["units"] = max(1, int(detail.get("units") or 1))
        source = str(detail.get("source") or "")
        if policy == "ignore_detached" and source == "process" and detail.get("detached"):
            return
        if policy == "wait_new" and not include_preexisting and not source.startswith("sensor_"):
            started_at = detail.get("started_at")
            if _background_detail_key(detail) in baseline:
                return
            if armed_at is not None and started_at is not None:
                try:
                    if float(started_at) <= float(armed_at):
                        return
                except (TypeError, ValueError):
                    pass
        details.append(detail)

    pending_events = 0
    try:
        from tools.process_registry import process_registry

        rows = process_registry.list_sessions()
        for row in rows:
            if row.get("status") != "running":
                continue
            add(
                {
                    "source": "process",
                    "id": row.get("session_id"),
                    "command": str(row.get("command") or "")[:160],
                    "detached": bool(row.get("detached")),
                    "started_at": _process_started_at(row, now),
                }
            )
        pending_events = max(0, int(process_registry.completion_queue.qsize()))
        if pending_events:
            add(
                {
                    "source": "completion_queue",
                    "id": "pending",
                    "command": "pending completion delivery",
                    "units": pending_events,
                }
            )
    except Exception:
        # Unknown is not zero. A broken sensor must veto real power actions.
        add(
            {
                "source": "sensor_process",
                "id": "sensor:process_registry",
                "command": "sensor unavailable",
            }
        )
        logger.debug("Could not inspect Hermes background processes", exc_info=True)

    try:
        from tools.async_delegation import list_async_delegations

        for record in list_async_delegations():
            if record.get("status") not in {"running", "stalling", "finalizing"}:
                continue
            goals = record.get("goals")
            units = len(goals) if record.get("is_batch") and isinstance(goals, (list, tuple)) and goals else 1
            add(
                {
                    "source": "delegation",
                    "id": record.get("delegation_id"),
                    "command": "subagent batch" if record.get("is_batch") else "subagent",
                    "units": units,
                    "started_at": record.get("dispatched_at"),
                }
            )
    except Exception:
        add(
            {
                "source": "sensor_delegation",
                "id": "sensor:async_delegation",
                "command": "sensor unavailable",
            }
        )
        logger.debug("Could not inspect async delegations", exc_info=True)

    try:
        from cron.scheduler import get_running_job_ids

        for job_id in get_running_job_ids():
            add({"source": "cron", "id": job_id, "command": "cron job"})
    except Exception:
        add({"source": "sensor_cron", "id": "sensor:cron", "command": "sensor unavailable"})
        logger.debug("Could not inspect running cron jobs", exc_info=True)

    processes = sum(
        int(item.get("units") or 1)
        for item in details
        if _background_detail_bucket(item) == "process"
    )
    delegations = sum(
        int(item.get("units") or 1)
        for item in details
        if _background_detail_bucket(item) == "delegation"
    )
    return processes, delegations, details


def _heartbeat() -> None:
    with _connect() as conn:
        settings = validate_settings(_meta_get(conn, "settings") or {})
    # Heartbeats publish raw observable work. Campaign scoping happens once in
    # _evaluate after rows from every process are deduplicated; filtering here
    # would make a baseline disappear, then reappear as falsely "new" work.
    processes, delegations, details = _background_snapshot(
        settings,
        include_preexisting=True,
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,
                active_delegations,process_details) VALUES(?,?,?,?,?,?)
            ON CONFLICT(owner_instance) DO UPDATE SET
                owner_pid=excluded.owner_pid, updated_at=excluded.updated_at,
                active_processes=excluded.active_processes,
                active_delegations=excluded.active_delegations,
                process_details=excluded.process_details
            """,
            (PROCESS_INSTANCE, os.getpid(), _now(), processes, delegations, _json(details)),
        )


def _power_capabilities() -> Dict[str, bool]:
    capabilities = {"sleep_available": False, "hibernate_available": False}
    if not IS_WINDOWS:
        return capabilities
    try:
        capabilities["sleep_available"] = bool(ctypes.windll.powrprof.IsPwrSuspendAllowed())
    except Exception:
        pass
    try:
        capabilities["hibernate_available"] = bool(ctypes.windll.powrprof.IsPwrHibernateAllowed())
    except Exception:
        pass
    return capabilities


def _user_idle_seconds() -> Optional[float]:
    if not IS_WINDOWS:
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, ((int(tick) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0)
    except Exception:
        return None


def _protected_processes_running(names: Iterable[str]) -> tuple[list[str], bool]:
    protected = {Path(str(name)).name.casefold() for name in names if str(name).strip()}
    if not protected:
        return [], True
    try:
        import psutil  # type: ignore

        found = set()
        for process in psutil.process_iter(["name"]):
            try:
                name = str(process.info.get("name") or "").casefold()
                if name in protected:
                    found.add(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(found), True
    except Exception:
        # Fail closed when the user explicitly configured protected processes.
        return [], False


def _eligible_terminal(outcome: str, settings: Dict[str, Any]) -> bool:
    if outcome == "completed":
        return True
    if settings.get("trigger_mode") != "done_or_blocked":
        return False
    if outcome == "blocked":
        return True
    if outcome == "failed":
        return bool(settings.get("include_failures"))
    if outcome == "interrupted":
        return bool(settings.get("include_interrupted"))
    return False


def _scope_background_details(
    settings: Dict[str, Any],
    runtime: Dict[str, Any],
    details: Iterable[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Apply the one-shot process baseline without weakening live work.

    Only long-lived terminal processes are baselined. Cron runs, subagents,
    and completion deliveries always block because a stable job/queue identity
    cannot prove that a later occurrence is the same pre-arm work.
    """
    items = [dict(item) for item in details if isinstance(item, dict)]
    if settings.get("background_process_policy") != "wait_new":
        return items
    raw_baseline = runtime.get("background_baseline") or {}
    baseline = {
        str(key): max(0, int(value or 0))
        for key, value in raw_baseline.items()
    } if isinstance(raw_baseline, dict) else {
        str(key): 1 for key in (runtime.get("background_baseline_ids") or [])
    }
    armed_at = runtime.get("armed_at")
    scoped: list[Dict[str, Any]] = []
    for item in items:
        key = _background_detail_key(item)
        source = key.split(":", 1)[0]
        if source != "process":
            scoped.append(item)
            continue
        units = max(1, int(item.get("units") or 1))
        if key not in baseline:
            # A timestamp alone is not proof of pre-arm observation (the
            # registry exposes second-level start times). Unknown identities
            # therefore block even when their coarse timestamp predates arm.
            scoped.append(item)
            continue
        started_at = item.get("started_at")
        if armed_at is not None and started_at is not None:
            try:
                if float(started_at) > float(armed_at):
                    scoped.append(item)
                    continue
                remaining = max(0, units - baseline[key])
            except (TypeError, ValueError):
                remaining = max(0, units - baseline[key])
        else:
            remaining = max(0, units - baseline[key])
        if remaining:
            item["units"] = remaining
            scoped.append(item)
    return scoped


def _aggregate_background_heartbeats(
    conn: sqlite3.Connection,
    settings: Dict[str, Any],
    runtime: Dict[str, Any],
    now: float,
) -> tuple[int, int, list[Dict[str, Any]]]:
    """Deduplicate and campaign-scope all fresh background observations."""
    stale_heartbeat = int(settings["stale_heartbeat_seconds"])
    heartbeat_rows = conn.execute(
        "SELECT * FROM heartbeats WHERE updated_at>=?", (now - stale_heartbeat,)
    ).fetchall()
    fallback_processes = max(
        (int(row["active_processes"] or 0) for row in heartbeat_rows),
        default=0,
    )
    fallback_delegations = max(
        (int(row["active_delegations"] or 0) for row in heartbeat_rows),
        default=0,
    )
    details_by_key: Dict[str, Dict[str, Any]] = {}
    for row in heartbeat_rows:
        try:
            row_details = json.loads(row["process_details"] or "[]")
        except (TypeError, ValueError):
            row_details = []
        if not row_details:
            owner = str(row["owner_instance"] or "unknown")
            if int(row["active_processes"] or 0):
                row_details.append(
                    {
                        "source": "sensor_process",
                        "id": f"sensor:legacy_process:{owner}",
                        "command": "legacy heartbeat without process identities",
                        "units": int(row["active_processes"] or 0),
                    }
                )
            if int(row["active_delegations"] or 0):
                row_details.append(
                    {
                        "source": "sensor_delegation",
                        "id": f"sensor:legacy_delegation:{owner}",
                        "command": "legacy heartbeat without delegation identities",
                        "units": int(row["active_delegations"] or 0),
                    }
                )
        for item in row_details:
            if not isinstance(item, dict):
                continue
            key = _background_detail_key(item)
            existing = details_by_key.get(key)
            if existing is None or int(item.get("units") or 1) > int(existing.get("units") or 1):
                details_by_key[key] = dict(item)

    background_details = _scope_background_details(
        settings, runtime, details_by_key.values()
    )

    detail_processes = sum(
        max(1, int(item.get("units") or 1))
        for item in background_details
        if _background_detail_bucket(item) == "process"
    )
    detail_delegations = sum(
        max(1, int(item.get("units") or 1))
        for item in background_details
        if _background_detail_bucket(item) == "delegation"
    )
    if settings.get("background_process_policy") == "wait_new" and details_by_key:
        return detail_processes, detail_delegations, background_details
    return (
        max(detail_processes, fallback_processes),
        max(detail_delegations, fallback_delegations),
        background_details,
    )


def _evaluate() -> Optional[Dict[str, Any]]:
    """Advance the shared state machine. Returns an action claim if due."""
    settings_probe = get_settings()
    protected_names_probe = tuple(settings_probe.get("protected_processes") or [])
    if settings_probe.get("armed") and protected_names_probe:
        protected_probe, process_scan_probe_ok = _protected_processes_running(protected_names_probe)
    else:
        protected_probe, process_scan_probe_ok = [], True
    now = _now()
    claim: Optional[Dict[str, Any]] = None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()

        # Simulated countdown is intentionally independent of armed state.
        if runtime.get("test_only") and runtime.get("state") == "countdown":
            due = float(runtime.get("countdown_due") or 0)
            if due and now >= due and not runtime.get("action_claimed_by"):
                runtime["action_claimed_by"] = PROCESS_INSTANCE
                runtime["state"] = "executing"
                runtime["updated_at"] = now
                claim = {
                    "token": runtime.get("countdown_token"),
                    "action": settings.get("action", "shutdown"),
                    "simulate": True,
                    "test_only": True,
                }
                _meta_set(conn, "runtime", runtime)
                _event(conn, "test_execute", "Claimed simulated power action")
            conn.commit()
            return claim

        if not settings.get("armed"):
            # The process that atomically claimed a due action disarms first so
            # no second action can ever be scheduled. Other monitor processes
            # may evaluate during the tiny execute/finalize window; they must
            # preserve `executing` and its countdown token or the legitimate
            # claimant could no longer finalize the result.
            if runtime.get("state") in {"executing", "action_requested", "action_complete", "error", "expired"}:
                conn.commit()
                return None
            if runtime.get("state") != "disarmed":
                runtime["state"] = "disarmed"
                runtime["quiet_since"] = None
                runtime["countdown_due"] = None
                runtime["countdown_token"] = ""
                _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        armed_expires_at = runtime.get("armed_expires_at")
        if armed_expires_at is not None and now >= float(armed_expires_at):
            settings["armed"] = False
            runtime["state"] = "expired"
            runtime["snooze_until"] = None
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = now
            _meta_set(conn, "settings", settings)
            _meta_set(conn, "runtime", runtime)
            _event(conn, "campaign_expired", "Armed campaign expired without a power action")
            conn.commit()
            return None

        generation = int(runtime.get("generation") or 0)
        stale_heartbeat = int(settings["stale_heartbeat_seconds"])
        stale_task = int(settings["stale_task_seconds"])
        waiting_timeout = int(settings["waiting_input_timeout_seconds"])

        # Waiting-for-user tasks become terminal blockers only after the custom
        # grace period. Running tasks are considered stalled only if their owner
        # heartbeat is gone too; a long model/tool call in a healthy process is
        # not mistaken for dead work.
        conn.execute(
            "UPDATE tasks SET status='blocked', outcome='blocked', "
            "reason='waiting_for_user_timeout', ended_at=?, updated_at=? "
            "WHERE generation=? AND status='waiting_input' AND updated_at<?",
            (now, now, generation, now - waiting_timeout),
        )
        conn.execute(
            """
            UPDATE tasks SET status='blocked', outcome='blocked',
                reason='owner_process_stalled', ended_at=?, updated_at=?
            WHERE generation=? AND status='running' AND updated_at<?
              AND owner_instance NOT IN (
                SELECT owner_instance FROM heartbeats WHERE updated_at>=?
              )
            """,
            (now, now, generation, now - stale_task, now - stale_heartbeat),
        )
        conn.execute("DELETE FROM heartbeats WHERE updated_at<?", (now - max(120, stale_heartbeat * 5),))

        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE generation=? ORDER BY started_at", (generation,)
        ).fetchall()
        active_tasks = [row for row in task_rows if row["status"] in {"running", "waiting_input"}]
        terminal_tasks = [row for row in task_rows if row["outcome"]]
        ineligible = [row for row in terminal_tasks if not _eligible_terminal(str(row["outcome"]), settings)]

        background_processes, delegations, background_details = _aggregate_background_heartbeats(
            conn, settings, runtime, now
        )

        runtime["active_tasks"] = len(active_tasks)
        runtime["active_background_processes"] = background_processes
        runtime["active_delegations"] = delegations
        runtime["background_details"] = background_details[:50]
        runtime["completed_terminal_count"] = sum(1 for row in terminal_tasks if row["outcome"] == "completed")
        runtime["blocked_terminal_count"] = sum(1 for row in terminal_tasks if row["outcome"] != "completed")
        desktop_busy_count = max(0, int(runtime.get("desktop_busy_count") or 0))
        runtime["desktop_busy_count"] = desktop_busy_count
        runtime["updated_at"] = now

        any_active = bool(active_tasks or background_processes or delegations or desktop_busy_count)
        if any_active:
            if runtime.get("state") == "countdown" and settings.get("cancel_on_new_activity", True):
                _event(conn, "countdown_cancelled", "Countdown cancelled: work became active")
            runtime["state"] = "running"
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        snooze_until = runtime.get("snooze_until")
        if snooze_until is not None:
            runtime["snooze_remaining_seconds"] = max(0, int(float(snooze_until) - now + 0.999))
            if now < float(snooze_until):
                runtime["state"] = "snoozed"
                runtime["quiet_since"] = None
                runtime["countdown_due"] = None
                runtime["countdown_reason"] = ""
                runtime["countdown_token"] = ""
                runtime["action_claimed_by"] = ""
                _meta_set(conn, "runtime", runtime)
                conn.commit()
                return None
            runtime["snooze_until"] = None
            runtime["snooze_remaining_seconds"] = None
            runtime["state"] = "armed_waiting_for_quiet"
            runtime["quiet_since"] = None
            _event(conn, "snooze_elapsed", "Delay elapsed; eligibility recomputed")

        ui_heartbeat_at = runtime.get("ui_heartbeat_at")
        ui_heartbeat_age = (
            now - float(ui_heartbeat_at) if ui_heartbeat_at is not None else None
        )
        runtime["ui_heartbeat_age_seconds"] = ui_heartbeat_age
        if ui_heartbeat_age is None or ui_heartbeat_age > UI_HEARTBEAT_TIMEOUT_SECONDS:
            if runtime.get("state") == "countdown":
                _event(conn, "countdown_cancelled", "Desktop UI heartbeat expired")
            runtime["state"] = "waiting_for_desktop_ui"
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        if not runtime.get("activity_seen"):
            runtime["state"] = "armed_waiting_for_task"
            runtime["quiet_since"] = None
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        if not terminal_tasks:
            runtime["state"] = "waiting_for_terminal_signal"
            runtime["quiet_since"] = None
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        if ineligible:
            runtime["state"] = "terminal_not_eligible"
            runtime["quiet_since"] = None
            runtime["ineligible"] = [
                {"task_key": row["task_key"], "outcome": row["outcome"], "reason": row["reason"]}
                for row in ineligible[:20]
            ]
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        if tuple(settings.get("protected_processes") or []) == protected_names_probe:
            protected, process_scan_ok = protected_probe, process_scan_probe_ok
        else:
            # Policy changed after the out-of-transaction sample. Retry on the
            # next monitor tick rather than scanning under the write lock.
            protected, process_scan_ok = [], False
        runtime["protected_processes_running"] = protected
        runtime["protected_process_scan_ok"] = process_scan_ok
        if protected or not process_scan_ok:
            runtime["state"] = "waiting_for_protected_process"
            runtime["quiet_since"] = None
            _meta_set(conn, "runtime", runtime)
            conn.commit()
            return None

        idle_seconds = _user_idle_seconds()
        runtime["user_idle_seconds"] = idle_seconds
        if settings.get("require_user_idle"):
            if idle_seconds is None:
                runtime["state"] = "waiting_for_idle_check"
                runtime["quiet_since"] = None
                _meta_set(conn, "runtime", runtime)
                conn.commit()
                return None
            if idle_seconds < int(settings["user_idle_seconds"]):
                runtime["state"] = "waiting_for_user_idle"
                runtime["quiet_since"] = None
                _meta_set(conn, "runtime", runtime)
                conn.commit()
                return None

        if runtime.get("state") == "countdown" and not runtime.get("test_only"):
            if idle_seconds is None:
                runtime["state"] = "waiting_for_idle_check"
                runtime["quiet_since"] = None
                runtime["countdown_due"] = None
                runtime["countdown_token"] = ""
                runtime["action_claimed_by"] = ""
                _meta_set(conn, "runtime", runtime)
                _event(conn, "countdown_cancelled", "User-input sensor unavailable")
                conn.commit()
                return None
            baseline_input_at = runtime.get("countdown_last_input_at")
            current_input_at = now - float(idle_seconds)
            if baseline_input_at is not None and current_input_at > float(baseline_input_at) + 1.0:
                runtime["state"] = "armed_waiting_for_quiet"
                runtime["quiet_since"] = None
                runtime["countdown_due"] = None
                runtime["countdown_reason"] = ""
                runtime["countdown_token"] = ""
                runtime["action_claimed_by"] = ""
                runtime["countdown_last_input_at"] = current_input_at
                _meta_set(conn, "runtime", runtime)
                _event(conn, "countdown_cancelled", "Countdown cancelled by keyboard or mouse input")
                conn.commit()
                return None

        if runtime.get("quiet_since") is None:
            runtime["quiet_since"] = now
            runtime["state"] = "quiescence"
            _meta_set(conn, "runtime", runtime)
            _event(conn, "quiescence", "All eligible work is terminal; quiet window started")
            conn.commit()
            return None

        quiet_elapsed = now - float(runtime.get("quiet_since") or now)
        if runtime.get("state") != "countdown" and quiet_elapsed >= int(settings["quiescence_seconds"]):
            runtime["state"] = "countdown"
            runtime["countdown_due"] = now + int(settings["countdown_seconds"])
            runtime["countdown_last_input_at"] = (
                now - float(idle_seconds) if idle_seconds is not None else None
            )
            runtime["countdown_reason"] = "all_work_terminal"
            runtime["countdown_token"] = uuid.uuid4().hex
            runtime["action_claimed_by"] = ""
            _meta_set(conn, "runtime", runtime)
            _event(
                conn,
                "countdown_started",
                f"{settings['action']} in {settings['countdown_seconds']}s",
                {"generation": generation},
            )
            conn.commit()
            return None

        if runtime.get("state") == "countdown":
            due = float(runtime.get("countdown_due") or 0)
            if due and now >= due and not runtime.get("action_claimed_by"):
                simulate = bool(settings.get("dry_run") or os.getenv("POWER_GUARD_TEST_MODE") == "1")
                if not simulate and os.getenv("HERMES_DESKTOP") != "1":
                    runtime["state"] = "waiting_for_desktop_backend"
                    runtime["quiet_since"] = None
                    runtime["countdown_due"] = None
                    runtime["countdown_reason"] = ""
                    runtime["countdown_token"] = ""
                    runtime["action_claimed_by"] = ""
                    _meta_set(conn, "runtime", runtime)
                    _event(conn, "countdown_cancelled", "Only a Desktop-managed backend may claim a real action")
                    conn.commit()
                    return None
                runtime["action_claimed_by"] = PROCESS_INSTANCE
                runtime["state"] = "executing"
                runtime["updated_at"] = now
                claim = {
                    "token": runtime.get("countdown_token"),
                    "action": settings["action"],
                    "simulate": simulate,
                    "test_only": False,
                }
                settings["armed"] = False
                _meta_set(conn, "settings", settings)
                _meta_set(conn, "runtime", runtime)
                _event(conn, "action_claimed", f"Claimed {settings['action']} action")
                conn.commit()
                return claim

        _meta_set(conn, "runtime", runtime)
        conn.commit()
    return claim


def _hidden_subprocess_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": 15}
    if IS_WINDOWS:
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _claim_still_safe(claim: Dict[str, Any]) -> bool:
    """Second phase: re-check the world after the SQLite action claim."""
    if claim.get("simulate"):
        return True

    # Give concurrently arriving user/turn events a chance to linearize their
    # cancellation before the irreversible OS call, then sample input as late
    # as practical. This is deliberately outside every SQLite transaction.
    time.sleep(0.35)
    settings_snapshot = get_settings()
    idle_seconds = _user_idle_seconds()
    protected, process_scan_ok = _protected_processes_running(
        settings_snapshot.get("protected_processes") or []
    )
    _, _, live_background_details = _background_snapshot(
        settings_snapshot,
        include_preexisting=True,
    )
    now = _now()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        reasons: list[str] = []
        token = str(claim.get("token") or "")
        if str(runtime.get("countdown_token") or "") != token:
            reasons.append("claim token was cancelled")
        if runtime.get("state") != "executing":
            reasons.append(f"state changed to {runtime.get('state')}")
        if runtime.get("action_claimed_by") != PROCESS_INSTANCE:
            reasons.append("claim owner changed")
        if settings.get("action") != claim.get("action"):
            reasons.append("action setting changed")

        heartbeat_at = runtime.get("ui_heartbeat_at")
        if heartbeat_at is None or now - float(heartbeat_at) > UI_HEARTBEAT_TIMEOUT_SECONDS:
            reasons.append("Desktop UI heartbeat expired")

        generation = int(runtime.get("generation") or 0)
        active_task_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE generation=? "
                "AND status IN ('running','waiting_input')",
                (generation,),
            ).fetchone()[0]
        )
        if active_task_count:
            reasons.append(f"{active_task_count} task(s) became active")

        background_processes, delegations, _ = _aggregate_background_heartbeats(
            conn, settings, runtime, now
        )
        if background_processes or delegations:
            reasons.append("background work became active")
        scoped_live_details = _scope_background_details(
            settings, runtime, live_background_details
        )
        if scoped_live_details:
            reasons.append("live background resample found active work")
        if int(runtime.get("desktop_busy_count") or 0):
            reasons.append("another Desktop session is busy")

        baseline_input_at = runtime.get("countdown_last_input_at")
        if idle_seconds is None:
            reasons.append("user-input sensor unavailable")
        elif baseline_input_at is not None and now - float(idle_seconds) > float(baseline_input_at) + 1.0:
            reasons.append("keyboard or mouse input occurred during countdown")

        if settings.get("require_user_idle"):
            if idle_seconds is None or idle_seconds < int(settings["user_idle_seconds"]):
                reasons.append("user is active")
        if protected:
            reasons.append("protected process is running")
        if not process_scan_ok:
            reasons.append("protected-process scan unavailable")

        if reasons:
            runtime["state"] = "disarmed"
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = now
            _meta_set(conn, "runtime", runtime)
            _event(conn, "action_aborted", "; ".join(reasons))
            conn.commit()
            return False
        conn.commit()
        return True


def _claim_token_is_current(claim: Dict[str, Any]) -> bool:
    """Last micro-check immediately adjacent to the irreversible OS call."""
    simulate = bool(claim.get("simulate"))
    settings_probe = get_settings()
    protected_names_probe = tuple(settings_probe.get("protected_processes") or [])
    if simulate:
        live_details: list[Dict[str, Any]] = []
        protected_probe, protected_probe_ok = [], True
        idle_probe: Optional[float] = None
    else:
        _, _, live_details = _background_snapshot(
            settings_probe,
            include_preexisting=True,
        )
        protected_probe, protected_probe_ok = _protected_processes_running(
            protected_names_probe
        )
        idle_probe = _user_idle_seconds()
    sampled_at = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        reasons: list[str] = []
        current = bool(
            str(runtime.get("countdown_token") or "") == str(claim.get("token") or "")
            and runtime.get("state") == "executing"
            and runtime.get("action_claimed_by") == PROCESS_INSTANCE
        )
        if not current:
            reasons.append("claim token or owner changed")
        if settings.get("action") != claim.get("action"):
            reasons.append("action setting changed after final safety check")
        expected_simulate = bool(
            claim.get("test_only")
            or settings.get("dry_run")
            or os.getenv("POWER_GUARD_TEST_MODE") == "1"
        )
        if simulate != expected_simulate:
            reasons.append("simulation policy changed after final safety check")
        if not simulate:
            heartbeat_at = runtime.get("ui_heartbeat_at")
            if heartbeat_at is None or sampled_at - float(heartbeat_at) > UI_HEARTBEAT_TIMEOUT_SECONDS:
                reasons.append("Desktop UI heartbeat expired after final safety check")
            if int(runtime.get("desktop_busy_count") or 0):
                reasons.append("another Desktop session became busy")
            generation = int(runtime.get("generation") or 0)
            active_tasks = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE generation=? "
                    "AND status IN ('running','waiting_input')",
                    (generation,),
                ).fetchone()[0]
            )
            if active_tasks:
                reasons.append("task became active after final safety check")
            if _scope_background_details(settings, runtime, live_details):
                reasons.append("background work appeared after final safety check")
            if tuple(settings.get("protected_processes") or []) != protected_names_probe:
                reasons.append("protected-process policy changed")
            elif protected_probe or not protected_probe_ok:
                reasons.append("protected process appeared after final safety check")
            baseline_input_at = runtime.get("countdown_last_input_at")
            if idle_probe is None:
                reasons.append("user-input sensor unavailable after final safety check")
            elif baseline_input_at is not None and sampled_at - float(idle_probe) > float(baseline_input_at) + 1.0:
                reasons.append("keyboard or mouse input occurred after final safety check")
            if settings.get("require_user_idle") and (
                idle_probe is None or idle_probe < int(settings["user_idle_seconds"])
            ):
                reasons.append("user became active after final safety check")
        if reasons:
            if current:
                runtime["state"] = "disarmed"
                runtime["countdown_due"] = None
                runtime["countdown_reason"] = ""
                runtime["countdown_token"] = ""
                runtime["action_claimed_by"] = ""
                runtime["updated_at"] = _now()
                _meta_set(conn, "runtime", runtime)
                _event(conn, "action_aborted", "; ".join(reasons))
            conn.commit()
            return False
        conn.commit()
        return True


def _execute_power_action(action: str, simulate: bool) -> tuple[bool, str]:
    if simulate:
        return True, f"SIMULATED {action}"
    if action == "notify":
        return True, "Notification-only action completed"
    if os.getenv("HERMES_DESKTOP") != "1":
        return False, "Refusing power action outside a Desktop-managed local backend"
    if not IS_WINDOWS:
        return False, f"Power action {action!r} is currently implemented for Windows only"
    try:
        if action == "shutdown":
            result = subprocess.run(
                ["shutdown.exe", "/s", "/t", "0", "/d", "p:0:0", "/c", "Hermes Power Guard: all tasks finished"],
                **_hidden_subprocess_kwargs(),
            )
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or f"shutdown.exe exit {result.returncode}").strip()
            return True, "Shutdown requested"
        if action == "lock":
            ok = bool(ctypes.windll.user32.LockWorkStation())
            return ok, "Workstation locked" if ok else "LockWorkStation failed"
        if action in {"sleep", "hibernate"}:
            hibernate = action == "hibernate"
            # ForceCritical=False: Windows/applications may veto suspension to
            # protect unsaved work. A power-saving feature must never force it.
            ok = bool(ctypes.windll.powrprof.SetSuspendState(hibernate, False, False))
            return ok, f"{action.title()} requested" if ok else f"SetSuspendState({action}) failed"
        return False, f"Unknown action: {action}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _finalize_action(claim: Dict[str, Any], success: bool, message: str) -> None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if str(runtime.get("countdown_token") or "") != str(claim.get("token") or ""):
            conn.rollback()
            return
        requested_only = bool(
            success
            and not claim.get("simulate")
            and claim.get("action") in {"sleep", "hibernate", "shutdown"}
        )
        runtime["state"] = "action_requested" if requested_only else "action_complete" if success else "error"
        runtime["last_action"] = {
            "action": claim.get("action"),
            "simulated": bool(claim.get("simulate")),
            "success": success,
            "outcome": "request_sent" if requested_only else "completed" if success else "failed",
            "message": message,
            "at": _now(),
        }
        runtime["last_error"] = "" if success else message
        runtime["countdown_due"] = None
        runtime["countdown_token"] = ""
        runtime["test_only"] = False
        runtime["updated_at"] = _now()
        _meta_set(conn, "runtime", runtime)
        event_kind = "action_requested" if requested_only else "action_complete" if success else "action_error"
        _event(conn, event_kind, message)
        conn.commit()


def _set_execution_state(active: bool) -> None:
    global _EXECUTION_STATE_ACTIVE
    if not IS_WINDOWS or active == _EXECUTION_STATE_ACTIVE:
        return
    with _LOCAL_POWER_LOCK:
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if active else ES_CONTINUOUS
            result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
            if result:
                _EXECUTION_STATE_ACTIVE = active
        except Exception:
            logger.debug("SetThreadExecutionState failed", exc_info=True)


def _set_process_priority(lower: bool) -> None:
    global _ORIGINAL_PRIORITY_CLASS
    if not IS_WINDOWS:
        return
    with _LOCAL_POWER_LOCK:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentProcess()
            current = int(kernel32.GetPriorityClass(handle))
            if _ORIGINAL_PRIORITY_CLASS is None and current:
                _ORIGINAL_PRIORITY_CLASS = current
            target = 0x00004000 if lower else (_ORIGINAL_PRIORITY_CLASS or 0x00000020)  # BELOW_NORMAL / NORMAL
            kernel32.SetPriorityClass(handle, target)
        except Exception:
            logger.debug("SetPriorityClass failed", exc_info=True)


def turn_off_display() -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Display power control is currently Windows-only"
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SYSCOMMAND = 0x0112
        SC_MONITORPOWER = 0xF170
        ctypes.windll.user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
        return True, "Display-off signal sent"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _current_power_plan() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        result = subprocess.run(["powercfg.exe", "/getactivescheme"], **_hidden_subprocess_kwargs())
        match = re.search(r"([0-9a-fA-F-]{36})", result.stdout or "")
        return match.group(1).lower() if match else ""
    except Exception:
        return ""


def _set_power_plan(plan: str) -> tuple[bool, str]:
    guid = POWER_PLAN_GUIDS.get(plan, plan if re.fullmatch(r"[0-9a-fA-F-]{36}", plan or "") else "")
    if not guid or not IS_WINDOWS:
        return False, "Power plan unavailable"
    try:
        result = subprocess.run(["powercfg.exe", "/setactive", guid], **_hidden_subprocess_kwargs())
        if result.returncode == 0:
            return True, guid.lower()
        return False, (result.stderr or result.stdout or f"powercfg exit {result.returncode}").strip()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _restore_power_plan(guid: str) -> None:
    if guid:
        ok, message = _set_power_plan(guid)
        _log_event(f"power_plan_restore: {ok} {message}")


def _clear_power_plan_runtime() -> None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        runtime["power_plan_previous"] = ""
        runtime["power_plan_applied"] = ""
        _meta_set(conn, "runtime", runtime)
        conn.commit()


def _recover_pending_power_plan(previous: str, desired: str) -> None:
    """Resolve a crash between powercfg success and SQLite finalization."""
    current = _current_power_plan()
    desired_guid = str(POWER_PLAN_GUIDS.get(desired, desired or "")).casefold()
    previous_cf = str(previous or "").casefold()
    if current and desired_guid and current.casefold() == desired_guid and previous:
        ok, message = _set_power_plan(previous)
    elif current and previous_cf and current.casefold() == previous_cf:
        ok, message = True, "pending apply had not changed the plan"
    else:
        # A user/program selected a third plan, or the plan cannot be read.
        # Never overwrite that newer intent with a stale recovery snapshot.
        ok, message = True, f"pending recovery skipped (current={current or 'unknown'})"
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        runtime["power_plan_applied"] = ""
        runtime["power_plan_previous"] = ""
        if not ok:
            runtime["last_error"] = f"power_plan_recovery: {message}"
        _meta_set(conn, "runtime", runtime)
        _event(conn, "power_plan_recovery", f"{ok}: {message}")
        conn.commit()


def _manage_global_power_plan(settings: Dict[str, Any], working: bool) -> None:
    # Serialize apply/restore with API cancellation in this canonical process.
    # The DB CAS remains the cross-process fence; only the Desktop coordinator
    # calls this function for real campaigns.
    with _LOCAL_POWER_LOCK:
        _manage_global_power_plan_locked(settings, working)


def _manage_global_power_plan_locked(settings: Dict[str, Any], working: bool) -> None:
    desired = str(settings.get("power_plan") or "unchanged")
    with _connect() as probe_conn:
        runtime_probe = _meta_get(probe_conn, "runtime") or _fresh_runtime()
    needs_apply_probe = bool(
        working and desired != "unchanged" and not runtime_probe.get("power_plan_applied")
    )
    previous_probe = _current_power_plan() if needs_apply_probe else ""
    action: Optional[tuple[str, str, str]] = None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if working and desired != "unchanged" and not runtime.get("power_plan_applied"):
            runtime["power_plan_previous"] = previous_probe
            runtime["power_plan_applied"] = "pending"
            _meta_set(conn, "runtime", runtime)
            action = ("apply", desired, "")
        elif not working and runtime.get("power_plan_applied") not in {"", "pending", "restoring"} and settings.get("restore_power_plan"):
            previous = str(runtime.get("power_plan_previous") or "")
            applied = str(runtime.get("power_plan_applied") or "")
            runtime["power_plan_applied"] = "restoring"
            _meta_set(conn, "runtime", runtime)
            action = ("restore", previous, applied)
        conn.commit()
    if not action:
        return
    kind, target, expected_current = action
    if kind == "restore":
        current = _current_power_plan()
        if not current or current.casefold() != expected_current.casefold():
            # The user (or another program) changed plans while Power Guard was
            # active, or the active plan cannot be read. Never overwrite that
            # newer intent with a stale restore snapshot.
            ok = True
            message = f"restore skipped (current={current or 'unknown'}, expected={expected_current})"
        else:
            ok, message = _set_power_plan(target)
    else:
        ok, message = _set_power_plan(target)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if kind == "apply":
            runtime["power_plan_applied"] = message if ok else ""
        else:
            runtime["power_plan_applied"] = "" if ok else expected_current
            if ok:
                runtime["power_plan_previous"] = ""
        if not ok:
            runtime["last_error"] = f"power_plan_{kind}: {message}"
        _meta_set(conn, "runtime", runtime)
        _event(conn, f"power_plan_{kind}", f"{ok}: {message}")
        conn.commit()


def _apply_local_energy_features(status: Dict[str, Any]) -> None:
    global _DISPLAY_TURNED_OFF_FOR_IDLE_WINDOW
    settings = status["settings"]
    runtime = status["runtime"]
    armed = bool(settings.get("armed"))
    working = armed and runtime.get("state") in {"running", "armed_waiting_for_quiet"}
    _set_execution_state(bool(working and settings.get("prevent_sleep_while_working")))
    _set_process_priority(bool(working and settings.get("lower_hermes_priority")))
    _manage_global_power_plan(settings, working)

    idle = _user_idle_seconds()
    if idle is not None and idle < 5:
        _DISPLAY_TURNED_OFF_FOR_IDLE_WINDOW = False
    if (
        working
        and settings.get("turn_off_display_when_idle")
        and idle is not None
        and idle >= int(settings.get("display_off_idle_seconds") or 300)
        and not _DISPLAY_TURNED_OFF_FOR_IDLE_WINDOW
    ):
        ok, _ = turn_off_display()
        _DISPLAY_TURNED_OFF_FOR_IDLE_WINDOW = ok


def _reset_timing_after_clock_discontinuity(reason: str) -> None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if settings.get("armed") and runtime.get("state") in {"quiescence", "countdown"}:
            runtime["state"] = "armed_waiting_for_quiet"
            runtime["quiet_since"] = None
            runtime["countdown_due"] = None
            runtime["countdown_reason"] = ""
            runtime["countdown_token"] = ""
            runtime["action_claimed_by"] = ""
            runtime["updated_at"] = _now()
            _meta_set(conn, "runtime", runtime)
            _event(conn, "clock_reset", reason)
        conn.commit()


def _monitor_loop() -> None:
    global _LAST_MONITOR_WALL, _LAST_MONITOR_MONOTONIC
    while not _MONITOR_STOP.wait(2.0):
        try:
            wall_now = time.time()
            mono_now = time.monotonic()
            if _LAST_MONITOR_WALL is not None and _LAST_MONITOR_MONOTONIC is not None:
                wall_elapsed = wall_now - _LAST_MONITOR_WALL
                mono_elapsed = mono_now - _LAST_MONITOR_MONOTONIC
                if mono_elapsed > 10.0 or abs(wall_elapsed - mono_elapsed) > 5.0:
                    _reset_timing_after_clock_discontinuity(
                        f"Monitor/clock discontinuity (wall={wall_elapsed:.1f}s, monotonic={mono_elapsed:.1f}s)"
                    )
            _LAST_MONITOR_WALL = wall_now
            _LAST_MONITOR_MONOTONIC = mono_now
            _heartbeat()
            can_coordinate = (
                os.getenv("HERMES_DESKTOP") == "1"
                or os.getenv("POWER_GUARD_TEST_MODE") == "1"
            )
            claim = _evaluate() if can_coordinate else None
            status = get_status(include_events=False)
            if can_coordinate:
                _apply_local_energy_features(status)
            if claim:
                if _claim_still_safe(claim) and _claim_token_is_current(claim):
                    success, message = _execute_power_action(str(claim["action"]), bool(claim["simulate"]))
                    _finalize_action(claim, success, message)
                _set_execution_state(False)
                _set_process_priority(False)
        except Exception:
            logger.exception("Power Guard monitor tick failed")


def start_monitor() -> None:
    global _MONITOR_STARTED
    if os.getenv("POWER_GUARD_DISABLE_MONITOR") == "1":
        return
    with _MONITOR_LOCK:
        if _MONITOR_STARTED:
            return
        _MONITOR_STARTED = True
        thread = threading.Thread(target=_monitor_loop, name="power-guard-monitor", daemon=True)
        thread.start()


def _decision_snapshot(settings: Dict[str, Any], runtime: Dict[str, Any], tasks: list[Dict[str, Any]]) -> Dict[str, Any]:
    state = str(runtime.get("state") or "disarmed")
    active_total = sum(
        max(0, int(runtime.get(key) or 0))
        for key in (
            "active_tasks",
            "active_background_processes",
            "active_delegations",
            "desktop_busy_count",
        )
    )
    terminal_count = sum(1 for task in tasks if task.get("outcome"))
    ui_age = runtime.get("ui_heartbeat_age_seconds")
    ui_ok = ui_age is not None and float(ui_age) <= UI_HEARTBEAT_TIMEOUT_SECONDS
    idle_seconds = runtime.get("user_idle_seconds")
    idle_ok = not settings.get("require_user_idle") or (
        idle_seconds is not None and float(idle_seconds) >= int(settings["user_idle_seconds"])
    )
    protected_ok = not runtime.get("protected_processes_running") and runtime.get(
        "protected_process_scan_ok", True
    )
    now = _now()
    active_tasks = int(runtime.get("active_tasks") or 0)
    background = int(runtime.get("active_background_processes") or 0)
    delegations = int(runtime.get("active_delegations") or 0)
    desktop_busy = int(runtime.get("desktop_busy_count") or 0)
    idle_remaining = max(
        0,
        int(settings.get("user_idle_seconds") or 0) - int(idle_seconds or 0),
    )
    quiet_remaining = max(
        0,
        int(settings.get("quiescence_seconds") or 0)
        - int(now - float(runtime.get("quiet_since") or now)),
    )
    ineligible_count = len(runtime.get("ineligible") or [])
    protected_names = ", ".join(runtime.get("protected_processes_running") or [])
    action_name = {
        "sleep": "睡眠",
        "shutdown": "关机",
        "hibernate": "休眠",
        "lock": "锁屏",
        "notify": "提醒",
    }.get(str(settings.get("action") or "sleep"), "电源动作")

    terminal_status = "blocked" if state == "terminal_not_eligible" else (
        "pass" if terminal_count and active_total == 0 else "wait"
    )
    safety_status = "pass" if ui_ok and idle_ok and protected_ok else "wait"
    quiet_status = "pass" if state in {"countdown", "executing", "action_requested", "action_complete"} else (
        "active" if state == "quiescence" else "wait"
    )
    action_status = "pass" if state in {"action_requested", "action_complete"} else (
        "blocked" if state in {"error", "expired"} else "active" if state in {"countdown", "executing"} else "wait"
    )

    armed_gate = "pass" if settings.get("armed") else "blocked" if state == "expired" else "wait"
    activity_gate = "pass" if runtime.get("activity_seen") else "wait"
    scope_current = runtime.get("scope_mode") == "current"
    work_gate = "pass" if runtime.get("activity_seen") and active_total == 0 else "active" if active_total else "wait"
    gates = [
        {
            "id": "armed",
            "label": "本次自动睡眠已启用" if armed_gate == "pass" else "本次自动睡眠已失效" if armed_gate == "blocked" else "本次自动睡眠未启用",
            "status": armed_gate,
            "detail": "已接管当前项目" if armed_gate == "pass" and scope_current else "等待下一个项目" if armed_gate == "pass" else "不会执行电源动作",
        },
        {
            "id": "activity",
            "label": "当前项目已接管" if activity_gate == "pass" and scope_current else "新任务已开始" if activity_gate == "pass" else "等待第一个任务",
            "status": activity_gate,
            "detail": f"已纳入 {runtime.get('captured_task_count') or 0} 个运行中任务" if activity_gate == "pass" and scope_current else "已记录本次任务" if activity_gate == "pass" else "当前没有运行中任务",
        },
        {
            "id": "work",
            "label": "Hermes 工作已结束" if work_gate == "pass" else "等待 Hermes 工作结束" if work_gate == "active" else "等待 Hermes 工作",
            "status": work_gate,
            "value": active_total,
            "detail": f"剩余 {active_total} 项" if active_total else "没有活动工作",
        },
        {
            "id": "terminal",
            "label": "任务结果不符合规则" if terminal_status == "blocked" else "任务结果符合规则" if terminal_status == "pass" else "等待任务结果",
            "status": terminal_status,
            "value": terminal_count,
            "detail": f"{ineligible_count} 个结果不符合规则" if ineligible_count else f"已确认 {terminal_count} 个结果",
        },
        {
            "id": "safety",
            "label": "桌面与输入检查通过" if safety_status == "pass" else "检查桌面与输入",
            "status": safety_status,
            "detail": "没有发现阻止条件" if safety_status == "pass" else "等待输入、窗口或进程条件",
        },
        {
            "id": "quiet",
            "label": "迟到任务检查完成" if quiet_status == "pass" else "正在检查迟到任务" if quiet_status == "active" else "等待迟到任务检查",
            "status": quiet_status,
            "detail": f"还需 {quiet_remaining} 秒" if quiet_status == "active" else "已完成" if quiet_status == "pass" else "尚未开始",
        },
        {
            "id": "action",
            "label": f"已发送{action_name}请求" if action_status == "pass" else f"正在发送{action_name}请求" if action_status == "active" else f"等待{action_name}请求",
            "status": action_status,
            "detail": "已发送" if action_status == "pass" else "等待前置条件",
        },
    ]

    running_parts = []
    if active_tasks:
        running_parts.append(f"会话 {active_tasks}")
    if background:
        running_parts.append(f"后台 {background}")
    if delegations:
        running_parts.append(f"子代理 {delegations}")
    if desktop_busy:
        running_parts.append(f"其他窗口 {desktop_busy}")
    running_detail = " · ".join(running_parts) or "等待 Hermes 更新任务状态"
    first_ineligible = (runtime.get("ineligible") or [{}])[0]
    ineligible_reason = str(first_ineligible.get("reason") or "未知或中断的结果")

    summaries = {
        "disarmed": (f"自动{action_name}未启用", "当前不会执行任何电源动作。"),
        "armed_waiting_for_task": ("等待下一个 Hermes 任务", f"启用时没有检测到当前运行中的任务；下一个任务结束后自动{action_name}。"),
        "running": (f"Hermes 还有 {active_total} 项工作", running_detail),
        "waiting_for_desktop_ui": ("控制面板已离线", "无法显示或取消倒计时，因此电脑保持唤醒。"),
        "waiting_for_terminal_signal": ("任务结束信号不完整", "没有可确认的完成或阻塞结果，因此电脑保持唤醒。"),
        "terminal_not_eligible": (f"{max(1, ineligible_count)} 个任务结果不符合规则", ineligible_reason),
        "waiting_for_protected_process": ("受保护程序仍在运行", protected_names or "进程检查尚未通过。"),
        "waiting_for_idle_check": ("读不到键鼠空闲时间", "没有输入证据，因此电脑保持唤醒。"),
        "waiting_for_user_idle": ("刚刚检测到键鼠操作", f"再空闲 {idle_remaining} 秒后开始迟到任务检查。"),
        "armed_waiting_for_quiet": ("条件发生变化", "旧倒计时已取消，正在重新检查全部条件。"),
        "quiescence": ("正在检查迟到任务", f"还需 {quiet_remaining} 秒没有新活动。"),
        "snoozed": (f"本次自动{action_name}已延后", f"{runtime.get('snooze_remaining_seconds') or 0} 秒后重新检查全部条件。"),
        "countdown": (f"{runtime.get('countdown_remaining_seconds') or 0} 秒后{action_name}", "键鼠输入、新任务或控制面板离线都会取消。"),
        "executing": (f"正在向 Windows 请求{action_name}", "正在做最后一次任务、输入和取消状态检查。"),
        "action_requested": (f"已向 Windows 请求{action_name}", "系统或应用仍可拒绝；Power Guard 不会自动重试。"),
        "action_complete": ("动作已完成", str((runtime.get("last_action") or {}).get("message") or "完成")),
        "expired": (f"本次自动{action_name}已失效", "未执行任何电源动作。需要时请重新启用。"),
        "error": ("Windows 没有接受电源请求", str(runtime.get("last_error") or "Power Guard 不会自动重试。")),
        "waiting_for_desktop_backend": ("等待本机 Desktop 后端", "只有本机 Hermes Desktop 可以发送真实电源请求。"),
    }
    next_steps = {
        "disarmed": f"保存规则后，启用本次自动{action_name}。",
        "armed_waiting_for_task": "无需操作；开始下一个任务后自动跟踪。",
        "running": "无需操作；所有工作结束后自动继续。",
        "waiting_for_desktop_ui": "重新打开 Power Guard 页面或恢复 Desktop 连接。",
        "waiting_for_terminal_signal": "保持唤醒，等待 Hermes 给出明确结果。",
        "terminal_not_eligible": "查看记录，或修改任务结果规则后重新启用。",
        "waiting_for_protected_process": "关闭受保护程序，或从规则中移除它。",
        "waiting_for_idle_check": "恢复 Windows 输入检测后自动继续。",
        "waiting_for_user_idle": "停止键鼠操作后自动继续。",
        "armed_waiting_for_quiet": "无需操作；确认期会重新开始。",
        "quiescence": "无需操作；确认完成后进入倒计时。",
        "snoozed": "可以取消延后，或等待到期。",
        "countdown": f"需要时点击“取消{action_name}”。",
        "executing": "无需操作。",
        "action_requested": "如果系统没有执行，请查看 Windows 电源设置或应用阻止原因。",
        "action_complete": "本次自动流程已结束。",
        "expired": "需要时重新启用。",
        "error": "查看错误记录并手动重新启用；不会自动重试。",
        "waiting_for_desktop_backend": "在本机 Hermes Desktop 中保持控制面板在线。",
    }
    summary, detail = summaries.get(state, ("状态无法识别", "电脑保持唤醒；请查看记录。"))
    return {
        "state": state,
        "summary": summary,
        "detail": detail,
        "next": next_steps.get(state, "保持唤醒并查看记录。"),
        "active_total": active_total,
        "counts": {
            "turns": active_tasks,
            "background": background,
            "delegations": delegations,
            "desktop_busy": desktop_busy,
        },
        "terminal_count": terminal_count,
        "gates": gates,
    }


def get_status(include_events: bool = True) -> Dict[str, Any]:
    with _connect() as conn:
        settings = validate_settings(_meta_get(conn, "settings") or {})
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        generation = int(runtime.get("generation") or 0)
        tasks = [dict(row) for row in conn.execute(
            "SELECT task_key,session_id,task_id,turn_id,platform,status,outcome,reason,last_tool,"
            "response_blocker,started_at,updated_at,ended_at FROM tasks "
            "WHERE generation=? ORDER BY started_at DESC LIMIT 100",
            (generation,),
        ).fetchall()]
        events: list[Dict[str, Any]] = []
        if include_events:
            for row in conn.execute(
                "SELECT id,created_at,kind,message,details FROM events ORDER BY id DESC LIMIT 50"
            ).fetchall():
                item = dict(row)
                try:
                    item["details"] = json.loads(item["details"] or "{}")
                except (TypeError, ValueError):
                    item["details"] = {}
                events.append(item)

    now = _now()
    countdown_remaining = None
    if runtime.get("countdown_due"):
        countdown_remaining = max(0, int(float(runtime["countdown_due"]) - now + 0.999))
    idle = _user_idle_seconds()
    if idle is not None:
        runtime["user_idle_seconds"] = int(idle)
    ui_heartbeat_at = runtime.get("ui_heartbeat_at")
    runtime["ui_heartbeat_age_seconds"] = (
        max(0.0, now - float(ui_heartbeat_at)) if ui_heartbeat_at is not None else None
    )
    armed_expires_at = runtime.get("armed_expires_at")
    runtime["armed_remaining_seconds"] = (
        max(0, int(float(armed_expires_at) - now + 0.999)) if armed_expires_at is not None else None
    )
    snooze_until = runtime.get("snooze_until")
    runtime["snooze_remaining_seconds"] = (
        max(0, int(float(snooze_until) - now + 0.999)) if snooze_until is not None else None
    )
    runtime["countdown_remaining_seconds"] = countdown_remaining
    power_capabilities = _power_capabilities()
    return {
        "ok": True,
        "plugin": PLUGIN_ID,
        "platform": platform.system(),
        "settings": settings,
        "runtime": runtime,
        "decision": _decision_snapshot(settings, runtime, tasks),
        "tasks": tasks,
        "events": events,
        "capabilities": {
            "power_actions": sorted(ALLOWED_ACTIONS),
            "windows_power_control": IS_WINDOWS,
            "desktop_managed_backend": os.getenv("HERMES_DESKTOP") == "1",
            "user_idle_detection": idle is not None,
            "snooze_supported": True,
            "decision_explanation_supported": True,
            "ui_contract_version": 2,
            **power_capabilities,
            "db_path": str(DB_PATH),
        },
    }


def reset_for_tests() -> None:
    """Clear mutable state. Intended only for the plugin's isolated test DB."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM heartbeats")
        conn.execute("DELETE FROM events")
        _meta_set(conn, "settings", DEFAULT_SETTINGS)
        _meta_set(conn, "runtime", _fresh_runtime())
        conn.commit()
