"""复盘会议领域服务：会议纪要登记、签认、分发，含跨模块决议分发到诊改任务。

分发稳定性要点（与 ``app.services.distribution`` 台账配合）：

1. 分发先逐项派发诊改任务并登记回执，**全部拿到成功回执才把纪要置「已分发」**；
   部分派发失败时纪要保持「已签认」并标记异常，分发页可认出是回到自己的入口续办。
2. 同一次请求的交付状态以台账为唯一事实来源，纪要页与诊改任务页读到的状态一致。
3. 失败后通过「补发分发」幂等续办，已成功的项不重复创建任务。
4. 交付成功后受理号、接手人回写到纪要，重看纪要仍能拿到原受理号与接手人。
"""
from __future__ import annotations

from typing import Any

from app.services import distribution as dist
from app.services.base import BaseModuleService
from app.store import store


class ReviewMeetingService(BaseModuleService):
    MODULE = "review_meeting"
    ENTITY = "会议纪要"
    OPERATION_TERM = "签认纪要"
    LIST_FIELDS = [
        "会议主题", "参会人员", "召开日期", "决议事项", "分歧观点", "记录人员",
        "下次会议", "备注说明", "分发状态", "分发进度", "受理号", "接手人",
    ]
    REQUIRED_FIELDS = ["会议主题", "参会人员", "召开日期"]
    STATUS_ORDER = ["草稿", "待签认", "已签认", "已分发"]
    ACTION_RULES = {"签认纪要": "已签认", "提出异议": "草稿", "分发给教师": "已分发"}
    EXTRA_ACTIONS = ["补发分发"]
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 查询
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
        """把交付台账中的办理进度投影到纪要视图，两页面据此保持同一口径。"""
        meeting_id = int(view.get("id", 0))
        delivery = dist.find_delivery(meeting_id)
        if delivery is None:
            view.setdefault("分发状态", "")
            view.setdefault("分发进度", "")
            view.setdefault("受理号", "")
            view.setdefault("接手人", "")
            return view
        view["分发状态"] = delivery["status"]
        view["分发进度"] = dist.progress_text(delivery)
        view["受理号"] = delivery.get("受理号", "")
        view["接手人"] = delivery.get("接手人", "")
        return view

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "签认纪要":
            if entry.get("status") not in ("待签认", "草稿"):
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可签认"
            return super().run_action(entry_id, action)

        if action == "分发给教师":
            return self._dispatch(entry_id, retry=False)

        if action == "补发分发":
            return self._dispatch(entry_id, retry=True)

        # 其它动作（如提出异议）走通用骨架
        result_entry, message = super().run_action(entry_id, action)
        return result_entry, message

    # ----------------------------------------------------------- 分发主流程
    def _dispatch(self, entry_id: int, *, retry: bool) -> tuple[dict[str, Any] | None, str]:
        # 重新取原始记录（非投影视图），保证拿到可写行
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        delivery = dist.find_delivery(entry_id)

        if not retry:
            # 已完成分发优先识别，避免被状态校验抢先，保证幂等提示准确
            if delivery is not None and delivery["status"] == dist.DELIVERY_DONE:
                return None, f"{self.ENTITY} {entry_id} 已完成分发（受理号 {delivery['受理号']}），无需重复分发"
            if entry.get("status") != "已签认":
                return None, f"{self.ENTITY} {entry_id} 必须先签认纪要，才能分发"
        else:
            if delivery is None:
                return None, f"{self.ENTITY} {entry_id} 尚无进行中的分发记录，不能补发；请先执行分发"
            if delivery["status"] == dist.DELIVERY_DONE:
                return None, f"{self.ENTITY} {entry_id} 分发已完成（受理号 {delivery['受理号']}），无需补发"
            if entry.get("status") != "已签认":
                return None, (
                    f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，"
                    "补发分发要求纪要仍处于已签认的待办入口"
                )

        planned = self._plan_items(entry)
        if not planned:
            return None, f"{self.ENTITY} {entry_id} 决议事项不含需要派发的诊改任务"

        # 断点续办：已存在进行中台账则复用，保留已拿到的回执
        delivery = dist.begin_delivery(entry_id, planned)
        had_failure = False

        for plan in delivery["items"]:
            if plan["status"] == dist.ITEM_SUCCESS:
                continue  # 幂等：已有成功回执的项跳过
            result = self._dispatch_one(entry, delivery, plan)
            if result is not None:
                had_failure = True

        completed = dist.complete_if_done(entry_id)
        refreshed = self.get_entry(entry_id)

        if completed is not None:
            # 全部回执齐全才推进纪要状态，并固化受理号、接手人
            entry["status"] = "已分发"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["受理号"] = completed["受理号"]
            entry["接手人"] = completed["接手人"]
            refreshed = self.get_entry(entry_id)
            message = (
                f"分发完成，受理号 {completed['受理号']}，"
                f"共派发 {completed['total']} 项诊改任务，接手人：{completed['接手人']}"
            )
            return refreshed, message

        # 部分失败：纪要保持「已签认」，标记异常，提示主任回到本入口续办
        entry["status"] = "已签认"
        entry["pending"] = True
        entry["abnormal"] = True
        refreshed = self.get_entry(entry_id)
        prefix = "补发后仍有派发失败" if retry else "部分决议派发失败"
        message = (
            f"{prefix}：{dist.progress_text(delivery)}。"
            "纪要保持已签认，请在本分发入口执行「补发分发」，或等待后台补发；"
            "未拿到成功回执的诊改任务暂不可认领。"
        )
        return refreshed, message

    def _plan_items(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """把决议事项拆成派发项：多条决议按行/分号拆分，含「任务」才派发。"""
        raw = str(entry.get("决议事项", "") or "").strip()
        if not raw or "任务" not in raw:
            return []
        chunks = [part.strip() for part in raw.replace("；", ";").split(";") if part.strip()]
        chunks = [c for c in chunks if "任务" in c] or [raw]
        theme = str(entry.get("会议主题", "") or "")
        receivers = self._split_people(entry.get("参会人员", ""))
        planned: list[dict[str, Any]] = []
        for seq, chunk in enumerate(chunks, start=1):
            planned.append({
                "item_key": f"resolution:{seq}",
                "seq": seq,
                "title": chunk,
                "receiver": receivers[(seq - 1) % len(receivers)] if receivers else "",
            })
        return planned

    @staticmethod
    def _split_people(value: Any) -> list[str]:
        text = str(value or "")
        for sep in ("、", ",", "，", ";", "；"):
            text = text.replace(sep, "|")
        return [part.strip() for part in text.split("|") if part.strip()]

    def _dispatch_one(
        self,
        entry: dict[str, Any],
        delivery: dict[str, Any],
        plan: dict[str, Any],
    ) -> str | None:
        """派发单个决议项，返回失败原因；成功返回 None。

        两个故障注入点对应两类中途故障：
        - before_write：任务尚未写入即失败；
        - after_write：任务已写入但回执未登记（缺回执）。
        """
        meeting_id = int(entry["id"])
        item_key = plan["item_key"]
        point_write = dist.failure_point(meeting_id, item_key, "before_write")
        point_receipt = dist.failure_point(meeting_id, item_key, "after_write")

        # 故障点1：任务写入前失败 —— 两端都不应出现可认领任务
        if dist.is_failing(point_write):
            dist.record_failure(meeting_id, item_key, "任务写入前中断，诊改任务未生成")
            return "任务写入前中断"

        # 断点恢复时若任务其实已落库（after_write 场景），复用而非重复创建
        task_id = plan.get("task_id")
        task = store.find("improve_task", int(task_id)) if task_id else None
        if task is None:
            task = self._create_task(entry, plan, delivery)

        # 故障点2：任务已写入、回执未登记 —— 任务存在但缺决议出处回执
        if dist.is_failing(point_receipt):
            dist.record_failure(
                meeting_id, item_key, "任务已写入但回执登记中断，缺派发回执",
                task_id=int(task["id"]),
            )
            return "回执登记中断"

        receiver = plan.get("receiver", "") or str(entry.get("记录人员", "") or "")
        dist.record_success(meeting_id, item_key, int(task["id"]), receiver)
        # 回写受理号与接手人到任务，作为认领时的决议出处凭证
        task["受理号"] = dist.item_acceptance_no(meeting_id, int(plan["seq"]))
        task["接手人"] = receiver
        task["出处纪要"] = dist.meeting_ref(meeting_id)
        return None

    def _create_task(
        self, entry: dict[str, Any], plan: dict[str, Any], delivery: dict[str, Any]
    ) -> dict[str, Any]:
        meeting_id = int(entry["id"])
        theme = str(entry.get("会议主题", "") or "")
        task_rows = store.rows("improve_task")
        new_id = max((int(r.get("id", 0)) for r in task_rows), default=0) + 1
        task = {
            "id": new_id,
            "任务描述": f"会议决议：{theme} [{dist.meeting_ref(meeting_id)}#{plan['item_key']}]",
            "承接方案": plan["title"],
            "执行人员": "",
            "起止日期": "",
            "验收标准": "",
            "督办人员": str(entry.get("记录人员", "") or ""),
            "任务权重": "",
            "备注说明": "由复盘会议决议自动生成，待分发回执确认后方可认领",
            "status": "待认领",
            "pending": True,
            "abnormal": False,
            "出处纪要": dist.meeting_ref(meeting_id),
            "受理号": "",
            "接手人": "",
        }
        task_rows.append(task)
        return task

    # ----------------------------------------------------------- 两端核对
    def reconcile(self, entry_id: int) -> dict[str, Any]:
        """核对纪要分发台账与诊改任务两端记录，识别两类中途故障。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise ValueError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        delivery = dist.find_delivery(entry_id)
        if delivery is None:
            return {
                "meeting_id": entry_id,
                "has_delivery": False,
                "status": "",
                "items": [],
                "mismatches": [],
                "message": "该纪要尚未发起分发，无两端记录可核对",
            }

        mismatches: list[dict[str, Any]] = []
        item_reports: list[dict[str, Any]] = []
        for item in delivery["items"]:
            task_id = item.get("task_id")
            task = store.find("improve_task", int(task_id)) if task_id else None
            report = {
                "item_key": item["item_key"],
                "受理号": item.get("受理号", ""),
                "receiver": item.get("receiver", ""),
                "delivery_status": item["status"],
                "task_id": task_id,
                "task_exists": task is not None,
                "task_status": task.get("status") if task else "",
            }
            if item["status"] == dist.ITEM_FAILED:
                if task is None:
                    report["issue"] = "任务尚未写入"
                    mismatches.append({
                        "item_key": item["item_key"],
                        "type": "missing_task",
                        "detail": "台账记录派发失败，诊改任务端无对应任务（任务尚未写入）",
                    })
                else:
                    report["issue"] = "缺少派发回执"
                    mismatches.append({
                        "item_key": item["item_key"],
                        "type": "missing_receipt",
                        "detail": "诊改任务已写入但台账无成功回执（完成后没收到回执）",
                        "task_id": task_id,
                    })
            elif item["status"] == dist.ITEM_SUCCESS and task is None:
                report["issue"] = "回执有记录但任务缺失"
                mismatches.append({
                    "item_key": item["item_key"],
                    "type": "orphan_receipt",
                    "detail": "台账显示派发成功但诊改任务端查无此任务",
                    "task_id": task_id,
                })
            item_reports.append(report)

        # 反向核对：该纪要自动生成的任务是否都能在台账中找到派发项
        ref = dist.meeting_ref(entry_id)
        for task in store.rows("improve_task"):
            if ref not in str(task.get("出处纪要", "")):
                continue
            known = any(i.get("task_id") == task["id"] for i in delivery["items"])
            if not known:
                mismatches.append({
                    "type": "untracked_task",
                    "detail": "诊改任务端存在本纪要生成的任务，但台账无对应派发项",
                    "task_id": task["id"],
                })

        return {
            "meeting_id": entry_id,
            "has_delivery": True,
            "status": delivery["status"],
            "受理号": delivery.get("受理号", ""),
            "接手人": delivery.get("接手人", ""),
            "items": item_reports,
            "mismatches": mismatches,
            "message": (
                "两端记录一致" if not mismatches
                else f"发现 {len(mismatches)} 处两端记录不一致，可执行补发分发修复"
            ),
        }
