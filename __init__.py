"""Hermes registration for Power Guard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import power_guard_core as core

_PROFILE_NAME = "default"


def _on_pre_llm_call(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    core.record_turn_start(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        platform_name=platform,
        profile=_PROFILE_NAME,
    )


def _on_pre_tool_call(
    tool_name: str = "",
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> None:
    core.record_tool_state(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        profile=_PROFILE_NAME,
        tool_name=tool_name,
        waiting=tool_name == "clarify",
    )


def _on_post_tool_call(
    tool_name: str = "",
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> None:
    core.record_tool_state(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        profile=_PROFILE_NAME,
        tool_name=tool_name,
        waiting=False,
    )


def _on_post_llm_call(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    assistant_response: str = "",
    **_: Any,
) -> None:
    core.record_response(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        profile=_PROFILE_NAME,
        response=assistant_response,
    )


def _on_session_end(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    completed: bool = False,
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: str = "",
    **_: Any,
) -> None:
    core.finish_turn(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        profile=_PROFILE_NAME,
        completed=completed,
        failed=failed,
        interrupted=interrupted,
        turn_exit_reason=turn_exit_reason,
    )


def _on_pre_approval_request(**_: Any) -> None:
    core.record_latest_owner_waiting("waiting_for_approval")


def _on_post_approval_response(choice: str = "", **_: Any) -> None:
    core.record_latest_owner_resumed(choice)


def _on_pre_gateway_dispatch(**_: Any) -> None:
    core.record_external_activity("gateway_dispatch")


def _on_kanban_claimed(
    task_id: str = "",
    board: str = "",
    profile_name: str = "",
    **_: Any,
) -> None:
    core.record_kanban(task_id, "running", board=board or "", profile=profile_name or "")


def _on_kanban_completed(
    task_id: str = "",
    board: str = "",
    profile_name: str = "",
    summary: Optional[str] = None,
    **_: Any,
) -> None:
    core.record_kanban(
        task_id,
        "completed",
        reason=(summary or "completed")[:500],
        board=board or "",
        profile=profile_name or "",
    )


def _on_kanban_blocked(
    task_id: str = "",
    board: str = "",
    profile_name: str = "",
    reason: Optional[str] = None,
    **_: Any,
) -> None:
    core.record_kanban(
        task_id,
        "blocked",
        reason=(reason or "blocked")[:500],
        board=board or "",
        profile=profile_name or "",
    )


def _on_kanban_worker_exited(
    task_id: str = "",
    board: str = "",
    profile_name: str = "",
    outcome: str = "",
    exit_kind: str = "",
    **_: Any,
) -> None:
    if outcome in {"crashed", "rate_limited"}:
        core.record_kanban(
            task_id,
            "blocked",
            reason=f"worker_{outcome}:{exit_kind}",
            board=board or "",
            profile=profile_name or "",
        )


def _status_text(status: Dict[str, Any]) -> str:
    settings = status["settings"]
    runtime = status["runtime"]
    lines = [
        "Power Guard",
        f"  armed: {settings['armed']}",
        f"  state: {runtime.get('state')}",
        f"  action: {settings['action']}",
        f"  active tasks: {runtime.get('active_tasks', 0)}",
        f"  background processes: {runtime.get('active_background_processes', 0)}",
        f"  delegations: {runtime.get('active_delegations', 0)}",
    ]
    remaining = runtime.get("countdown_remaining_seconds")
    if remaining is not None:
        lines.append(f"  countdown: {remaining}s")
    if runtime.get("last_error"):
        lines.append(f"  error: {runtime['last_error']}")
    return "\n".join(lines)


def _handle_command(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    command = parts[0].lower() if parts else "status"
    if command in {"status", "show"}:
        return _status_text(core.get_status())
    if command == "arm":
        return _status_text(core.arm())
    if command in {"cancel", "disarm", "stop"}:
        return _status_text(core.cancel("slash_command"))
    if command == "snooze":
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
        return _status_text(core.snooze(minutes))
    if command == "resume":
        return _status_text(core.resume_snooze())
    if command == "test":
        seconds = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8
        return _status_text(core.start_test_countdown(seconds))
    if command == "display-off":
        ok, message = core.turn_off_display()
        return f"Power Guard: {message}" if ok else f"Power Guard error: {message}"
    return (
        "Usage: /power-guard [status|arm|cancel|snooze [minutes]|resume|test [seconds]|display-off]\n"
        "Configure detailed rules in Desktop → 自动关机."
    )


def register(ctx) -> None:
    global _PROFILE_NAME
    _PROFILE_NAME = str(getattr(ctx, "profile_name", "default") or "default")
    core.initialize_desktop_backend()
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    ctx.register_hook("post_approval_response", _on_post_approval_response)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("kanban_task_claimed", _on_kanban_claimed)
    ctx.register_hook("kanban_task_completed", _on_kanban_completed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_blocked)
    ctx.register_hook("on_kanban_worker_exited", _on_kanban_worker_exited)
    ctx.register_command(
        "power-guard",
        handler=_handle_command,
        description="Arm, inspect, delay, test, or cancel automatic power actions.",
        args_hint="[status|arm|cancel|snooze [minutes]|resume|test [seconds]|display-off]",
    )
    core.start_monitor()
