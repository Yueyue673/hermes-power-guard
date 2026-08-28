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
    "background_process_policy": "wait_all",  # wait_all | ignore_detached
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
ALLOWED_BACKGROUND_POLICIES = {"wait_all", "ignore_detached"}
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
    if _meta_get(conn, "settings") is None:
        _meta_set(conn, "settings", DEFAULT_SETTINGS)
    if _meta_get(conn, "runtime") is None:
        _meta_set(conn, "runtime", _fresh_runtime())


def _fresh_runtime() -> Dict[str, Any]:
    return {
        "generation": 0,
        "state": "disarmed",
        "armed_at": None,
        "armed_expires_at": None,
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
        if current.get("armed") and policy_changed:
            # A confirmed campaign is a snapshot of the exact policy the user
            # reviewed. Any setting edit invalidates that consent, so disarm and
            # require the stronger confirmation flow again.
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
            _event(conn, "settings_disarm", "Settings changed; confirmation is required again")
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


def arm(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = validate_settings(_meta_get(conn, "settings") or {})
        if overrides:
            settings = validate_settings(overrides, base=settings)
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        ui_heartbeat_at = runtime.get("ui_heartbeat_at")
        generation = int(runtime.get("generation") or 0) + 1
        settings["armed"] = True
        armed_at = _now()
        runtime = _fresh_runtime()
        runtime.update(
            {
                "generation": generation,
                "state": "armed_waiting_for_task",
                "armed_at": armed_at,
                "armed_expires_at": armed_at + int(settings["arm_expiry_minutes"]) * 60,
                "activity_seen": False,
                "ui_heartbeat_at": ui_heartbeat_at,
            }
        )
        _meta_set(conn, "settings", settings)
        _meta_set(conn, "runtime", runtime)
        _event(conn, "armed", f"Armed generation {generation}", {"action": settings["action"]})
        conn.commit()
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


def _background_snapshot(settings: Dict[str, Any]) -> tuple[int, int, list[Dict[str, Any]]]:
    processes = 0
    delegations = 0
    pending_events = 0
    details: list[Dict[str, Any]] = []
    try:
        from tools.process_registry import process_registry

        rows = process_registry.list_sessions()
        for row in rows:
            if row.get("status") != "running":
                continue
            if settings.get("background_process_policy") == "ignore_detached" and row.get("detached"):
                continue
            processes += 1
            details.append(
                {
                    "id": row.get("session_id"),
                    "command": str(row.get("command") or "")[:160],
                    "detached": bool(row.get("detached")),
                }
            )
        # A child/process can be finished but its completion event still needs
        # to re-enter the parent conversation. Until the shared queue drains,
        # Hermes has automatic follow-up work left and shutdown is premature.
        pending_events = int(process_registry.completion_queue.qsize())
    except Exception:
        # Unknown is not zero. A broken sensor must veto real power actions
        # until the next successful sample rather than silently hiding work.
        processes += 1
        details.append({"id": "sensor:process_registry", "command": "sensor unavailable", "detached": False})
        logger.debug("Could not inspect Hermes background processes", exc_info=True)
    try:
        from tools.async_delegation import active_task_count

        delegations = int(active_task_count()) + pending_events
    except Exception:
        delegations = pending_events + 1
        details.append({"id": "sensor:async_delegation", "command": "sensor unavailable", "detached": False})
        logger.debug("Could not inspect async delegations", exc_info=True)
    try:
        from cron.scheduler import get_running_job_ids

        cron_ids = list(get_running_job_ids())
        processes += len(cron_ids)
        details.extend({"id": job_id, "command": "cron job", "detached": False} for job_id in cron_ids)
    except Exception:
        processes += 1
        details.append({"id": "sensor:cron", "command": "sensor unavailable", "detached": False})
        logger.debug("Could not inspect running cron jobs", exc_info=True)
    return processes, delegations, details


def _heartbeat() -> None:
    settings = get_settings()
    processes, delegations, details = _background_snapshot(settings)
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


def _evaluate() -> Optional[Dict[str, Any]]:
    """Advance the shared state machine. Returns an action claim if due."""
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

        heartbeat_rows = conn.execute(
            "SELECT * FROM heartbeats WHERE updated_at>=?", (now - stale_heartbeat,)
        ).fetchall()
        background_processes = sum(int(row["active_processes"] or 0) for row in heartbeat_rows)
        delegations = sum(int(row["active_delegations"] or 0) for row in heartbeat_rows)
        background_details: list[Dict[str, Any]] = []
        for row in heartbeat_rows:
            try:
                background_details.extend(json.loads(row["process_details"] or "[]"))
            except (TypeError, ValueError):
                pass

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

        protected, process_scan_ok = _protected_processes_running(settings.get("protected_processes") or [])
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

        stale_heartbeat = int(settings["stale_heartbeat_seconds"])
        bg_row = conn.execute(
            "SELECT COALESCE(SUM(active_processes),0), COALESCE(SUM(active_delegations),0) "
            "FROM heartbeats WHERE updated_at>=?",
            (now - stale_heartbeat,),
        ).fetchone()
        if int(bg_row[0] or 0) or int(bg_row[1] or 0):
            reasons.append("background work became active")
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
    with _connect() as conn:
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        return bool(
            str(runtime.get("countdown_token") or "") == str(claim.get("token") or "")
            and runtime.get("state") == "executing"
            and runtime.get("action_claimed_by") == PROCESS_INSTANCE
        )


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
    action: Optional[tuple[str, str, str]] = None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = _meta_get(conn, "runtime") or _fresh_runtime()
        if working and desired != "unchanged" and not runtime.get("power_plan_applied"):
            previous = _current_power_plan()
            runtime["power_plan_previous"] = previous
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

    gates = [
        {"id": "armed", "label": "已确认武装", "status": "pass" if settings.get("armed") else "blocked" if state == "expired" else "wait"},
        {"id": "activity", "label": "已观察到新任务", "status": "pass" if runtime.get("activity_seen") else "wait"},
        {"id": "work", "label": "自动工作已清空", "status": "pass" if runtime.get("activity_seen") and active_total == 0 else "active" if active_total else "wait", "value": active_total},
        {"id": "terminal", "label": "终态符合规则", "status": terminal_status, "value": terminal_count},
        {"id": "safety", "label": "用户与桌面安全门", "status": safety_status},
        {"id": "quiet", "label": "静默确认完成", "status": quiet_status},
        {"id": "action", "label": "最终动作", "status": action_status},
    ]

    summaries = {
        "disarmed": ("尚未启用", "确认设置后武装，Power Guard 才会观察之后开始的新任务。"),
        "armed_waiting_for_task": ("等待新任务", "武装前已经存在的旧任务不会触发自动电源动作。"),
        "running": ("Hermes 仍在工作", f"还有 {active_total} 个活动工作单元；完成后会自动进入安全门。"),
        "waiting_for_desktop_ui": ("等待桌面控制面板", "Desktop 心跳不可见，已禁止进入倒计时。"),
        "waiting_for_terminal_signal": ("等待终态信号", "任务看似空闲，但还没有足够证据判断完成或阻塞。"),
        "terminal_not_eligible": ("当前终态不允许执行", "存在中断、未知结果，或不符合你选择的完成规则。"),
        "waiting_for_protected_process": ("受保护程序仍在运行", "关闭或移出保护列表后会重新判断。"),
        "waiting_for_idle_check": ("无法确认用户空闲", "读取不到系统空闲状态时会保持唤醒，不会冒险执行。"),
        "waiting_for_user_idle": ("等待你离开电脑", f"需要连续空闲 {int(settings['user_idle_seconds']) // 60} 分钟。"),
        "armed_waiting_for_quiet": ("重新确认中", "新活动或设置变化撤销了旧倒计时，正在重新对账。"),
        "quiescence": ("静默确认中", f"保持无新工作 {settings['quiescence_seconds']} 秒后进入倒计时。"),
        "snoozed": ("已延后", f"剩余 {runtime.get('snooze_remaining_seconds') or 0} 秒后重新判断。"),
        "countdown": ("最终倒计时", f"剩余 {runtime.get('countdown_remaining_seconds') or 0} 秒；任何活动都会取消。"),
        "executing": ("正在执行最终动作", "已经进入最后复核阶段。"),
        "action_requested": ("系统请求已发送", "Windows 已接受请求；实际睡眠结果由系统和应用决定。"),
        "action_complete": ("动作已完成", str((runtime.get("last_action") or {}).get("message") or "完成")),
        "expired": ("武装已过期", "长时间没有完成动作，已自动解除武装。"),
        "error": ("执行失败", str(runtime.get("last_error") or "查看事件记录获取详情。")),
    }
    summary, detail = summaries.get(state, (state, "Power Guard 正在重新计算当前状态。"))
    return {
        "state": state,
        "summary": summary,
        "detail": detail,
        "active_total": active_total,
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
