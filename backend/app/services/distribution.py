"""决议分发交付台账：受理号、逐项派发回执与故障恢复的一致性记录。

复盘纪要分发诊改任务属于跨模块写操作，中途可能在"任务写入"或"回执登记"
任一环节故障。本模块把一次分发落成一条可重入的台账：

- 纪要侧与诊改任务侧读取的是同一条台账，两个页面的交付状态天然一致；
- 每个派发项分别记录 待派发/派发成功/派发失败，失败项可幂等补发；
- 只有全部派发项拿到成功回执，台账才置「已完成」并生成受理号、接手人；
- ``failure_points`` 是演练/测试用的故障注入点，键形如
  ``meeting:45:resolution:1:before_write``。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "distribution_record"

DELIVERY_IN_PROGRESS = "进行中"
DELIVERY_DONE = "已完成"
ITEM_PENDING = "待派发"
ITEM_SUCCESS = "派发成功"
ITEM_FAILED = "派发失败"

# 故障注入点（进程内、显式配置），模拟任务写入前/回执登记前的中途故障。
_failure_points: set[str] = set()


def configure_failure(point: str, failing: bool = True) -> None:
    """配置或解除一个故障注入点。"""
    if failing:
        _failure_points.add(point)
    else:
        _failure_points.discard(point)


def clear_failures() -> None:
    """清除全部故障注入点。"""
    _failure_points.clear()


def is_failing(point: str) -> bool:
    return point in _failure_points


def failure_point(meeting_id: int, item_key: str, phase: str) -> str:
    """统一故障点命名：meeting:{纪要id}:{派发项}:{before_write|after_write}。"""
    return f"meeting:{meeting_id}:{item_key}:{phase}"


def _rows() -> list[dict[str, Any]]:
    return store.rows(MODULE)


def meeting_ref(meeting_id: int) -> str:
    return f"meeting:{meeting_id}"


def acceptance_no(meeting_id: int) -> str:
    """整次分发的受理号，交付成功后固定不变，重看纪要仍取到原号。"""
    return f"RM{meeting_id:04d}"


def item_acceptance_no(meeting_id: int, seq: int) -> str:
    return f"{acceptance_no(meeting_id)}-{seq:02d}"


def find_delivery(meeting_id: int) -> dict[str, Any] | None:
    """按纪要 ID 查找分发台账。"""
    ref = meeting_ref(meeting_id)
    for row in _rows():
        if row.get("会议纪要") == ref:
            return row
    return None


def find_item_by_task(task_id: int) -> dict[str, Any] | None:
    """按诊改任务 ID 反查派发项，供认领时校验决议出处。"""
    for delivery in _rows():
        for item in delivery.get("items", []):
            if item.get("task_id") == task_id:
                return item
    return None


def find_delivery_by_task(task_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """按诊改任务 ID 反查所属台账与派发项。"""
    for delivery in _rows():
        for item in delivery.get("items", []):
            if item.get("task_id") == task_id:
                return delivery, item
    return None, None


def begin_delivery(meeting_id: int, planned_items: list[dict[str, Any]]) -> dict[str, Any]:
    """开始或续接一次分发。

    已存在「进行中」台账时原样返回（保留已有回执，支持断点续办）；
    已完成的台账直接返回；否则新建。
    """
    existing = find_delivery(meeting_id)
    if existing is not None:
        return existing

    new_id = max((int(r.get("id", 0)) for r in _rows()), default=0) + 1
    delivery = {
        "id": new_id,
        "会议纪要": meeting_ref(meeting_id),
        "status": DELIVERY_IN_PROGRESS,
        "受理号": "",
        "接手人": "",
        "total": len(planned_items),
        "succeeded": 0,
        "failed": 0,
        "items": [
            {
                "item_key": item["item_key"],
                "seq": item["seq"],
                "title": item.get("title", ""),
                "receiver": item.get("receiver", ""),
                "受理号": "",
                "status": ITEM_PENDING,
                "task_id": None,
                "reason": "",
            }
            for item in planned_items
        ],
        "异常": len(planned_items) > 0,
        "备注说明": "分发进行中，等待全部派发回执",
    }
    _rows().append(delivery)
    return delivery


def _find_item(delivery: dict[str, Any], item_key: str) -> dict[str, Any] | None:
    for item in delivery.get("items", []):
        if item.get("item_key") == item_key:
            return item
    return None


def record_success(
    meeting_id: int, item_key: str, task_id: int, receiver: str
) -> dict[str, Any] | None:
    """登记一项派发成功回执（幂等：已成功的项不重复计数）。"""
    delivery = find_delivery(meeting_id)
    if delivery is None:
        return None
    item = _find_item(delivery, item_key)
    if item is None:
        return None
    if item["status"] != ITEM_SUCCESS:
        item["status"] = ITEM_SUCCESS
        item["task_id"] = task_id
        item["receiver"] = receiver or item.get("receiver", "")
        item["受理号"] = item_acceptance_no(meeting_id, int(item["seq"]))
        item["reason"] = ""
    _refresh_counts(delivery)
    return item


def record_failure(
    meeting_id: int, item_key: str, reason: str, task_id: int | None = None
) -> dict[str, Any] | None:
    """登记一项派发失败及原因；任务可能已写入（缺回执）也可能未写入。"""
    delivery = find_delivery(meeting_id)
    if delivery is None:
        return None
    item = _find_item(delivery, item_key)
    if item is None:
        return None
    item["status"] = ITEM_FAILED
    item["reason"] = reason
    if task_id is not None:
        item["task_id"] = task_id
    _refresh_counts(delivery)
    return item


def _refresh_counts(delivery: dict[str, Any]) -> None:
    items = delivery.get("items", [])
    delivery["succeeded"] = sum(1 for i in items if i["status"] == ITEM_SUCCESS)
    delivery["failed"] = sum(1 for i in items if i["status"] == ITEM_FAILED)
    delivery["异常"] = delivery["status"] != DELIVERY_DONE and (
        delivery["failed"] > 0
        or any(i["status"] == ITEM_PENDING for i in items)
    )


def complete_if_done(meeting_id: int) -> dict[str, Any] | None:
    """全部派发项均有成功回执时收尾，生成受理号与接手人；否则返回 None。"""
    delivery = find_delivery(meeting_id)
    if delivery is None or delivery["status"] == DELIVERY_DONE:
        return delivery
    items = delivery.get("items", [])
    if items and all(i["status"] == ITEM_SUCCESS for i in items):
        delivery["status"] = DELIVERY_DONE
        delivery["受理号"] = acceptance_no(meeting_id)
        receivers = [str(i.get("receiver", "")).strip() for i in items if i.get("receiver")]
        delivery["接手人"] = "、".join(dict.fromkeys(receivers))
        delivery["异常"] = False
        delivery["备注说明"] = "全部决议派发成功，回执齐全"
        return delivery
    delivery["备注说明"] = (
        f"分发未完成：成功 {delivery['succeeded']}/{delivery['total']}，"
        f"失败 {delivery['failed']} 项，待补发"
    )
    delivery["异常"] = True
    return None


def progress_text(delivery: dict[str, Any]) -> str:
    """给纪要列表/详情用的一句话办理进度。"""
    total = delivery.get("total", 0)
    succeeded = delivery.get("succeeded", 0)
    failed = delivery.get("failed", 0)
    pending = total - succeeded - failed
    parts = [f"已受理 {succeeded}/{total}"]
    if failed:
        parts.append(f"失败 {failed} 项")
    if pending:
        parts.append(f"待派发 {pending} 项")
    if delivery.get("status") == DELIVERY_DONE:
        parts = [f"派发成功 {total}/{total}"]
    return "，".join(parts)
