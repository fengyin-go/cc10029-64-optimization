"""复盘纪要分发中途故障的稳定性与恢复测试。

覆盖需求场景：
- 两个页面（纪要页 / 诊改任务页）对同一请求的交付状态保持一致；
- 只有拿到完整分发结果（全部派发回执成功），诊改任务才能认领；
- M-45 部分派发失败：纪要停在分发入口可续办，教师无法认领缺出处的任务；
- 重看纪要能拿到交付成功时的原受理号与接手人；
- 任务尚未写入、完成后没收到回执两类故障的两端记录核对与补发恢复。
"""
from __future__ import annotations

import pytest

from app.services import distribution as dist
from app.services.improve_task import ImproveTaskService
from app.services.review_meeting import ReviewMeetingService
from app.store import store


def clear_all() -> None:
    for name in store.module_names():
        store.rows(name).clear()
    dist.clear_failures()


def make_meeting(meeting_id: int, *, resolution: str | None = None) -> int:
    """以固定 id 落一条已签认纪要（M-45 场景直接用 45）。"""
    rows = store.rows("review_meeting")
    rows.append({
        "id": meeting_id,
        "status": "已签认",
        "pending": True,
        "abnormal": False,
        "会议主题": f"M-{meeting_id} 期中复盘",
        "参会人员": "王老师、李老师",
        "召开日期": "2026-11-20",
        "决议事项": resolution or "成立课堂互动改进任务组；建设课程资源更新任务",
        "分歧观点": "",
        "记录人员": "教研室主任",
        "下次会议": "",
        "备注说明": "",
    })
    return meeting_id


def meeting_tasks(meeting_id: int) -> list[dict]:
    ref = f"meeting:{meeting_id}"
    return [t for t in store.rows("improve_task") if ref in str(t.get("出处纪要", ""))]


@pytest.fixture(autouse=True)
def reset():
    clear_all()
    yield
    clear_all()


class TestHappyPathDelivery:
    def setup_method(self):
        self.meeting_svc = ReviewMeetingService()
        self.task_svc = ImproveTaskService()

    def test_full_delivery_marks_distributed_and_claimable(self):
        mid = make_meeting(100)
        entry, message = self.meeting_svc.run_action(mid, "分发给教师")

        # 纪要推进到已分发，台账完成
        assert entry["status"] == "已分发"
        assert entry["分发状态"] == dist.DELIVERY_DONE
        assert entry["受理号"] == "RM0100"
        assert entry["接手人"] == "王老师、李老师"
        assert "受理号 RM0100" in message

        # 两项任务均生成且带决议出处凭证
        tasks = meeting_tasks(mid)
        assert len(tasks) == 2
        for task in tasks:
            assert task["出处纪要"] == "meeting:100"
            assert task["受理号"].startswith("RM0100-")
            assert task["接手人"] in ("王老师", "李老师")

        # 完整交付后任务可以认领
        result, msg = self.task_svc.run_action(tasks[0]["id"], "认领任务")
        assert result is not None, msg
        assert result["status"] == "执行中"

    def test_reopen_shows_same_status_on_both_pages(self):
        mid = make_meeting(101)
        self.meeting_svc.run_action(mid, "分发给教师")

        # 重新进入纪要页与诊改任务页，读到的是同一份台账事实
        meeting_view = self.meeting_svc.get_entry(mid)
        progress = self.meeting_svc.reconcile(mid)
        tasks = meeting_tasks(mid)

        assert meeting_view["分发状态"] == progress["status"] == dist.DELIVERY_DONE
        assert meeting_view["受理号"] == progress["受理号"] == "RM0101"
        assert all(t["受理号"] for t in tasks)
        assert progress["mismatches"] == []

    def test_acceptance_number_and_receiver_stable_on_review(self):
        mid = make_meeting(102)
        self.meeting_svc.run_action(mid, "分发给教师")
        first = self.meeting_svc.get_entry(mid)
        first_no, first_owner = first["受理号"], first["接手人"]

        # 教师认领、推进任务后，主任重看纪要仍拿到原受理号与接手人
        tasks = meeting_tasks(mid)
        self.task_svc.run_action(tasks[0]["id"], "认领任务")
        again = self.meeting_svc.get_entry(mid)
        assert again["受理号"] == first_no
        assert again["接手人"] == first_owner

    def test_duplicate_dispatch_is_rejected_after_completion(self):
        mid = make_meeting(103)
        self.meeting_svc.run_action(mid, "分发给教师")
        result, msg = self.meeting_svc.run_action(mid, "分发给教师")
        assert result is None
        assert "无需重复分发" in msg
        assert len(meeting_tasks(mid)) == 2


class TestPartialFailureBeforeWrite:
    """故障点 before_write：任务尚未写入即中断。"""

    def setup_method(self):
        self.meeting_svc = ReviewMeetingService()
        self.task_svc = ImproveTaskService()

    def test_meeting_stays_at_entry_and_task_not_claimable(self):
        mid = 45
        make_meeting(mid)
        # 第一项写入前故障
        dist.configure_failure(dist.failure_point(mid, "resolution:1", "before_write"))

        entry, message = self.meeting_svc.run_action(mid, "分发给教师")

        # 纪要不推进，保持已签认并标异常，提示回到分发入口续办
        assert entry["status"] == "已签认"
        assert entry["abnormal"] is True
        assert entry["分发状态"] == dist.DELIVERY_IN_PROGRESS
        assert "补发分发" in message
        delivery = dist.find_delivery(mid)
        assert delivery["succeeded"] == 1
        assert delivery["failed"] == 1

        # 故障项无任务，成功项有任务但因整次未完成仍不可认领
        tasks = meeting_tasks(mid)
        assert len(tasks) == 1
        result, msg = self.task_svc.run_action(tasks[0]["id"], "认领任务")
        assert result is None
        assert "分发未完成" in msg

    def test_reconcile_flags_missing_task_then_resend_recovers(self):
        mid = 46
        make_meeting(mid)
        dist.configure_failure(dist.failure_point(mid, "resolution:1", "before_write"))
        self.meeting_svc.run_action(mid, "分发给教师")

        report = self.meeting_svc.reconcile(mid)
        kinds = {m["type"] for m in report["mismatches"]}
        assert "missing_task" in kinds
        assert report["status"] == dist.DELIVERY_IN_PROGRESS

        # 解除故障后补发：只补失败项，不重复建任务
        dist.clear_failures()
        before_ids = {t["id"] for t in meeting_tasks(mid)}
        entry, message = self.meeting_svc.run_action(mid, "补发分发")

        assert entry["status"] == "已分发"
        assert entry["受理号"] == "RM0046"
        assert len(meeting_tasks(mid)) == 2
        assert before_ids.issubset({t["id"] for t in meeting_tasks(mid)})
        assert self.meeting_svc.reconcile(mid)["mismatches"] == []
        assert "受理号 RM0046" in message

        # 恢复后任务可认领
        new_task = next(t for t in meeting_tasks(mid) if t["id"] not in before_ids)
        ok, _ = self.task_svc.run_action(new_task["id"], "认领任务")
        assert ok is not None

    def test_resend_without_delivery_is_rejected(self):
        mid = 47
        make_meeting(mid)
        result, msg = self.meeting_svc.run_action(mid, "补发分发")
        assert result is None
        assert "尚无进行中的分发记录" in msg


class TestPartialFailureAfterWrite:
    """故障点 after_write：任务已写入但回执未登记（缺回执）。"""

    def setup_method(self):
        self.meeting_svc = ReviewMeetingService()
        self.task_svc = ImproveTaskService()

    def test_written_task_without_receipt_cannot_be_claimed(self):
        mid = 48
        make_meeting(mid)
        dist.configure_failure(dist.failure_point(mid, "resolution:2", "after_write"))
        entry, _ = self.meeting_svc.run_action(mid, "分发给教师")

        # 两项任务都已写入，但第二项缺回执，整次分发未完成
        assert entry["status"] == "已签认"
        tasks = meeting_tasks(mid)
        assert len(tasks) == 2
        missing_receipt = [t for t in tasks if not t["受理号"]]
        assert len(missing_receipt) == 1

        for task in tasks:
            result, msg = self.task_svc.run_action(task["id"], "认领任务")
            assert result is None
            assert "不能认领" in msg

    def test_reconcile_flags_missing_receipt_and_resend_keeps_task(self):
        mid = 49
        make_meeting(mid)
        dist.configure_failure(dist.failure_point(mid, "resolution:1", "after_write"))
        self.meeting_svc.run_action(mid, "分发给教师")

        report = self.meeting_svc.reconcile(mid)
        mismatch = next(m for m in report["mismatches"] if m["type"] == "missing_receipt")
        orphan_task_id = mismatch["task_id"]
        assert orphan_task_id is not None

        # 补发复用已写入任务，回填同一受理号，不新建
        dist.clear_failures()
        entry, _ = self.meeting_svc.run_action(mid, "补发分发")
        assert entry["status"] == "已分发"
        tasks = meeting_tasks(mid)
        assert len(tasks) == 2
        recovered = store.find("improve_task", orphan_task_id)
        assert recovered is not None
        assert recovered["受理号"].startswith("RM0049-")

        # 恢复后原任务可认领，受理号保持为交付成功时生成的号
        ok, _ = self.task_svc.run_action(orphan_task_id, "认领任务")
        assert ok is not None
        assert self.meeting_svc.get_entry(mid)["受理号"] == "RM0049"

    def test_progress_endpoint_reflects_partial_state(self):
        mid = 50
        make_meeting(mid)
        dist.configure_failure(dist.failure_point(mid, "resolution:1", "before_write"))
        self.meeting_svc.run_action(mid, "分发给教师")

        delivery = dist.find_delivery(mid)
        assert delivery["status"] == dist.DELIVERY_IN_PROGRESS
        assert dist.progress_text(delivery).startswith("已受理 1/2")
        statuses = {i["item_key"]: i["status"] for i in delivery["items"]}
        assert statuses["resolution:1"] == dist.ITEM_FAILED
        assert statuses["resolution:2"] == dist.ITEM_SUCCESS


class TestClaimSourceGuard:
    def setup_method(self):
        self.meeting_svc = ReviewMeetingService()
        self.task_svc = ImproveTaskService()

    def test_task_with_meeting_source_but_no_ledger_rejected(self):
        # 构造一个带出处纪要但台账里查无派发项的任务，模拟来源不明工作
        rows = store.rows("improve_task")
        rows.append({
            "id": 900,
            "任务描述": "会议决议：来历不明 [meeting:999#resolution:1]",
            "承接方案": "缺失出处的决议",
            "执行人员": "",
            "出处纪要": "meeting:999",
            "受理号": "",
            "接手人": "",
            "status": "待认领",
            "pending": True,
            "abnormal": False,
        })
        result, msg = self.task_svc.run_action(900, "认领任务")
        assert result is None
        assert "缺少决议出处" in msg

    def test_manual_task_without_meeting_source_still_claimable(self):
        task, missing = self.task_svc.create_entry({
            "任务描述": "人工登记的常规诊改任务",
            "承接方案": "日常改进",
            "执行人员": "赵老师",
        })
        assert missing == []
        result, msg = self.task_svc.run_action(task["id"], "认领任务")
        assert result is not None, msg
        assert result["status"] == "执行中"

    def test_resolution_without_task_keyword_creates_nothing(self):
        mid = make_meeting(51, resolution="本次会议仅作情况通报，无后续安排")
        result, msg = self.meeting_svc.run_action(mid, "分发给教师")
        assert result is None
        assert "不含需要派发的诊改任务" in msg
        assert meeting_tasks(mid) == []


class TestTwoPageStatusConsistency:
    """重新进入纪要页与诊改任务页时，同一请求的交付状态必须一致。"""

    def setup_method(self):
        self.meeting_svc = ReviewMeetingService()
        self.task_svc = ImproveTaskService()

    def test_failed_delivery_shows_pending_on_both_pages(self):
        mid = 45
        make_meeting(mid)
        dist.configure_failure(dist.failure_point(mid, "resolution:1", "before_write"))
        self.meeting_svc.run_action(mid, "分发给教师")

        meeting_view = self.meeting_svc.get_entry(mid)
        assert meeting_view["分发状态"] == dist.DELIVERY_IN_PROGRESS

        task_views, _ = self.task_svc.list_entries()
        sourced = [t for t in task_views if str(t.get("出处纪要", "")) == f"meeting:{mid}"]
        # 成功写入的那项任务在任务页同样显示「分发未完成」，与纪要页口径一致
        assert sourced and all(t["交付状态"] == "分发未完成" for t in sourced)

    def test_completed_delivery_shows_done_on_both_pages(self):
        mid = 52
        make_meeting(mid)
        self.meeting_svc.run_action(mid, "分发给教师")

        meeting_view = self.meeting_svc.get_entry(mid)
        assert meeting_view["分发状态"] == dist.DELIVERY_DONE

        task_views, _ = self.task_svc.list_entries()
        sourced = [t for t in task_views if str(t.get("出处纪要", "")) == f"meeting:{mid}"]
        assert len(sourced) == 2
        assert all(t["交付状态"] == "交付完成" for t in sourced)

    def test_manual_task_marked_routine_and_still_claimable(self):
        task, _ = self.task_svc.create_entry({
            "任务描述": "常规人工任务",
            "承接方案": "日常改进",
            "执行人员": "赵老师",
        })
        view = self.task_svc.get_entry(task["id"])
        assert view["交付状态"] == "常规登记"
        ok, _ = self.task_svc.run_action(task["id"], "认领任务")
        assert ok is not None

    def test_reconcile_clean_after_full_delivery(self):
        mid = 53
        make_meeting(mid)
        self.meeting_svc.run_action(mid, "分发给教师")
        report = self.meeting_svc.reconcile(mid)
        assert report["mismatches"] == []
        assert report["status"] == dist.DELIVERY_DONE
        assert report["受理号"] == "RM0053"
        assert {item["task_exists"] for item in report["items"]} == {True}
