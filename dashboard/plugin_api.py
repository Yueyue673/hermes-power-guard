"""FastAPI routes shared by Hermes Desktop's Power Guard panel."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import power_guard_core as core  # noqa: E402

router = APIRouter()


@router.get("/status")
async def status() -> Dict[str, Any]:
    core.record_ui_heartbeat()
    return core.get_status()


@router.post("/ui-heartbeat")
async def ui_heartbeat(body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    busy_count = int((body or {}).get("busy_count") or 0)
    instance_id = str((body or {}).get("instance_id") or "desktop-status")
    core.record_ui_heartbeat(busy_count=busy_count, instance_id=instance_id)
    return core.get_status()


@router.post("/settings")
async def settings(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        core.update_settings(body)
        return core.get_status()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/arm")
async def arm(body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = dict(body or {})
    current_session_id = str(payload.pop("current_session_id", "") or "")
    current_profile = str(payload.pop("current_profile", "") or "")
    return core.arm(
        payload,
        current_session_id=current_session_id,
        current_profile=current_profile,
    )


@router.post("/cancel")
async def cancel(body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    reason = str((body or {}).get("reason") or "desktop_cancel")
    return core.cancel(reason)


@router.post("/snooze")
async def snooze(body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    minutes = int((body or {}).get("minutes") or 15)
    try:
        return core.snooze(minutes)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/resume")
async def resume() -> Dict[str, Any]:
    try:
        return core.resume_snooze()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/test")
async def test_countdown(body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    seconds = int((body or {}).get("seconds") or 8)
    try:
        return core.start_test_countdown(seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/display-off")
async def display_off() -> Dict[str, Any]:
    ok, message = core.turn_off_display()
    if not ok:
        raise HTTPException(status_code=501, detail=message)
    return {"ok": True, "message": message}
