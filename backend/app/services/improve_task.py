"""诊改任务领域服务：任务分解、认领、执行、验收，含跨模块关联方案校验。

认领稳定性要点：由复盘纪要分发产生的诊改任务，必须在交付台账中存在对应派发项
且该派发项已拿到成功回执（决议出处齐全）才允许认领；任务尚未写入或缺少派发
回执的工作不能获准，避免教师从待认领栏取到一次未完成分发的决议。
"""
from __future__ import annotations

from typing import Any

from app.services import distribution as dist
from app.services.base import BaseModuleService
from app.services.cross_module import check_entity_status


class ImproveTaskService(BaseModuleService):
    MODULE = "improve_task"
    ENTITY = "诊改任务"
    OPERATION_TERM = "认领任务"
    LIST_FIELDS = [
        "任务描述", "承接方案", "执行人员", "起止日期", "验收标准", "督办人员",
        "任务权重", "备注说明", "出处纪要", "受理号", "接手人", "交付状态",
    ]
    REQUIRED_FIELDS = ["任务描述", "承接方案", "执行人员"]
    STATUS_ORDER = ["草稿", "待认领", "执行中", "已验收"]
    ACTION_RULES = {"认领任务": "执行中", "启动执行": "执行中", "申请验收": "已验收"}
    NEGATIVE_ACTIONS = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        items, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size
        )
        return [self._with_delivery_view(dict(item)) for item in items], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        return self._with_delivery_view(dict(entry))

    def _with_delivery_view(self, view: dict[str, Any]) -> dict[str, Any]:
        """把同一份分发台账的交付状态投影到任务视图，与纪要页口径一致。"""
        source = str(view.get("出处纪要", "") or "").strip()
        if not source.startswith("meeting:"):
            view["交付状态"] = "常规登记"
            return view
        delivery, item = dist.find_delivery_by_task(int(view.get("id", 0)))
        if delivery is None or item is None:
            view["交付状态"] = "分发未完成"
        elif delivery.get("status") == dist.DELIVERY_DONE and item.get("status") == dist.ITEM_SUCCESS:
            view["交付状态"] = "交付完成"
        else:
            view["交付状态"] = "分发未完成"
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建诊改任务：承接方案引用必须是已审核通过的改进方案。"""
        source_plan = str(values.get("承接方案", "") or "").strip()

        # 尝试解析方案ID引用
        if source_plan.startswith("plan:"):
            plan_id_str = source_plan.split(":", 1)[1]
            try:
                plan_id = int(plan_id_str)
                check_entity_status(
                    "improve_plan", plan_id, ["审核通过"],
                    "诊改任务的承接方案"
                )
            except ValueError:
                pass

        return super().create_entry(values)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含状态推进约束与决议出处校验。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 认领任务：只能从待认领状态认领
        if action == "认领任务":
            if entry.get("status") not in ("待认领", "草稿"):
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可认领"
            source_error = self._check_resolution_source(entry_id, entry)
            if source_error:
                return None, source_error

        # 申请验收：必须已在执行中
        if action == "申请验收":
            if entry.get("status") != "执行中":
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，必须先执行再申请验收"

        return super().run_action(entry_id, action)

    def _check_resolution_source(
        self, entry_id: int, entry: dict[str, Any]
    ) -> str | None:
        """校验由复盘纪要分发产生的任务是否具备完整决议出处。

        - 任务带有 ``出处纪要`` 却查不到派发台账：分发未完成，不能认领；
        - 台账整体未完成或该派发项未成功：缺少决议出处/回执，不能认领；
        - 人工登记、无纪要出处的普通任务不受此约束。
        """
        source = str(entry.get("出处纪要", "") or "").strip()
        if not source:
            return None  # 非会议分发任务，按普通流程认领

        if not source.startswith("meeting:"):
            return None

        try:
            meeting_id = int(source.split(":", 1)[1])
        except ValueError:
            return f"{self.ENTITY} {entry_id} 的决议出处'{source}'无法识别，不能认领"

        delivery, item = dist.find_delivery_by_task(entry_id)
        if delivery is None or item is None:
            return (
                f"{self.ENTITY} {entry_id} 缺少决议出处：所属纪要 #{meeting_id} "
                "分发尚未完成或无派发回执，不能认领，请等待后台补发"
            )
        if delivery.get("status") != dist.DELIVERY_DONE:
            return (
                f"{self.ENTITY} {entry_id} 的分发未完成（{dist.progress_text(delivery)}），"
                "决议出处回执不齐全，不能认领"
            )
        if item.get("status") != dist.ITEM_SUCCESS:
            return (
                f"{self.ENTITY} {entry_id} 的派发项状态为'{item.get('status')}'，"
                "缺少成功派发回执，不能认领"
            )
        return None
