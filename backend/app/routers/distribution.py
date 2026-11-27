"""复盘纪要分发恢复接口：办理进度、补发分发、两端记录核对。

与通用资源路由分离，避免把恢复语义塞进通用动作接口；路由注册顺序上先于
``resource``，确保固定前缀优先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult
from app.services import distribution as dist
from app.services.review_meeting import ReviewMeetingService

router = APIRouter(prefix="/api/review_meeting", tags=["决议分发恢复"])

_service = ReviewMeetingService()


@router.get("/{entry_id}/delivery")
def delivery_progress(entry_id: int) -> dict[str, Any]:
    """查看某次分发的办理进度与逐项派发回执。"""
    entry = _service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"会议纪要 {entry_id} 不存在或已归档")
    delivery = dist.find_delivery(entry_id)
    if delivery is None:
        return {
            "meeting_id": entry_id,
            "in_progress": False,
            "message": "该纪要尚未发起分发",
            "items": [],
        }
    return {
        "meeting_id": entry_id,
        "in_progress": delivery["status"] != dist.DELIVERY_DONE,
        "status": delivery["status"],
        "受理号": delivery.get("受理号", ""),
        "接手人": delivery.get("接手人", ""),
        "progress": dist.progress_text(delivery),
        "items": delivery.get("items", []),
    }


@router.post("/{entry_id}/resend", response_model=ActionResult)
def resend_delivery(entry_id: int) -> ActionResult:
    """对部分派发失败的分发执行幂等补发，已成功的派发项不重复建任务。"""
    entry, message = _service.run_action(entry_id, "补发分发")
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/{entry_id}/reconcile")
def reconcile_delivery(entry_id: int) -> dict[str, Any]:
    """核对纪要分发台账与诊改任务两端记录，定位任务未写入/缺回执等故障。"""
    try:
        return _service.reconcile(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
