import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, ref, readings, **extra):
        payload = {"external_ref": ref, "readings": readings}
        payload.update(extra)
        return self.service.submit_batch(payload, "patrol", "inspector")

    def test_batch_carries_multiple_sections_and_opens_priority_tasks(self):
        batch = self._batch("B-1", [
            {"section": "12#坝段", "metric": "seepage", "value": 5,
             "control_value": 10, "external_ref": "R-1"},
            {"section": "13#坝段", "metric": "displacement", "value": 18,
             "control_value": 10, "external_ref": "R-2"},
            {"section": "14#坝段", "metric": "crack", "value": 25,
             "control_value": 10, "external_ref": "R-3"},
        ])
        self.assertEqual(batch["counts"]["total"], 3)
        self.assertEqual(batch["counts"]["normal"], 1)
        self.assertEqual(batch["counts"]["priority"], 2)
        self.assertTrue(batch["in_priority_queue"])
        # 每个优先读数都有责任人和到场时限
        roles = {t["assignee_role"] for t in batch["tasks"]}
        self.assertEqual(roles, {"dam_engineer", "emergency_manager"})
        for task in batch["tasks"]:
            self.assertTrue(task["assignee"])
            self.assertTrue(task["deadline"])
        # 超限缺陷直接处于已登记状态并要求复检
        reading = [r for r in batch["readings"] if r["external_ref"] == "R-2"][0]
        item = self.service.get_item(reading["item_id"], "viewer")
        self.assertEqual(item["status"], "inspected")
        self.assertTrue(item["requires_reinspection"])
        self.assertGreaterEqual(item["priority"], 6)

    def test_reinspection_required_before_close(self):
        batch = self._batch("B-2", [
            {"section": "13#坝段", "metric": "displacement", "value": 18,
             "control_value": 10, "external_ref": "R-9"},
        ])
        item_id = batch["readings"][0]["item_id"]
        current = self.service.get_item(item_id, "viewer")
        for target in ("defect_confirmed", "repair", "verified"):
            current = self.service.transition(
                current["id"], target, current["version"], "rev",
                TRANSITION_ROLES[target][0])
        # 复检记录缺失不能关闭
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "closed", current["version"],
                                    "mgr", "emergency_manager")
        # open的普通事项会同时触发未关闭事项拦截
        self.service.add_record(current["id"], {"kind": "action",
            "detail": "处置中", "status": "open", "external_ref": "ACT-0"}, "p", "inspector")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "closed", current["version"],
                                    "mgr", "emergency_manager")
        self.repo.conn.execute(
            "UPDATE records SET status='closed' WHERE item_id=? AND external_ref='ACT-0'",
            (current["id"],))
        self.repo.conn.commit()
        # 复检仍缺失，继续拦截；补一条closed复检记录后允许关闭
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "closed", current["version"],
                                    "mgr", "emergency_manager")
        self.service.add_record(current["id"], {"kind": "reinspection",
            "detail": "复检合格", "status": "closed", "external_ref": "RC-1"}, "p", "inspector")
        current = self.service.get_item(current["id"], "viewer")
        closed = self.service.transition(current["id"], "closed", current["version"],
                                         "mgr", "emergency_manager")
        self.assertEqual(closed["status"], "closed")

    def test_duplicate_refs_merge_on_earliest_report_conflicting_goes_review(self):
        first = self._batch("B-3", [
            {"section": "13#坝段", "metric": "displacement", "value": 18,
             "control_value": 10, "external_ref": "DUP"},
        ])
        first_id = first["readings"][0]["item_id"]
        second = self._batch("B-4", [
            # 一致 -> 合并，不产生新缺陷/任务
            {"section": "13#坝段", "metric": "displacement", "value": 18.5,
             "control_value": 10, "external_ref": "DUP"},
        ])
        states = {r["external_ref"]: r for r in second["readings"]}
        self.assertEqual(states["DUP"]["state"], "merged")
        self.assertEqual(states["DUP"]["item_id"], first_id)
        # 第三个批次同号到达，超限状态翻转的矛盾读数 -> 待复核
        third = self._batch("B-6", [
            {"section": "13#坝段", "metric": "displacement", "value": 9,
             "control_value": 10, "external_ref": "DUP"},
        ])
        conflict_reading = third["readings"][0]
        self.assertEqual(conflict_reading["state"], "review")
        self.assertTrue(conflict_reading["conflict_note"])
        # 基准读数累计连续异常次数（一致读数才累计）
        canonical = self.repo.find_canonical_reading("DUP")
        self.assertEqual(canonical["repeat_count"], 2)
        # 同缺陷只生成过一个处置任务
        self.assertEqual(len(self.service.list_tasks("viewer", first_id)), 1)

    def test_earlier_report_takes_over_as_canonical(self):
        self._batch("B-6", [
            {"section": "13#坝段", "metric": "crack", "value": 18,
             "control_value": 10, "external_ref": "EARLY",
             "reported_at": "2026-09-25T10:00:00+00:00"},
        ])
        self._batch("B-7", [
            {"section": "13#坝段", "metric": "crack", "value": 17,
             "control_value": 10, "external_ref": "EARLY",
             "reported_at": "2026-09-20T08:00:00+00:00"},
        ])
        canonical = self.repo.find_canonical_reading("EARLY")
        self.assertEqual(canonical["value"], 17)
        self.assertTrue(canonical["reported_at"].startswith("2026-09-20"))

    def test_queue_filters_and_task_claim_permissions(self):
        self._batch("B-8", [
            {"section": "12#", "metric": "seepage", "value": 4,
             "control_value": 10, "external_ref": "OK"},
        ])
        bad = self._batch("B-9", [
            {"section": "13#", "metric": "displacement", "value": 18,
             "control_value": 10, "external_ref": "BAD"},
        ])
        review = self._batch("B-10", [
            {"section": "13#", "metric": "displacement", "value": 4,
             "control_value": 10, "external_ref": "BAD"},
        ])
        priority_batches = {b["external_ref"]
                            for b in self.service.list_batches("viewer", "priority")}
        review_batches = {b["external_ref"]
                          for b in self.service.list_batches("viewer", "review")}
        all_batches = {b["external_ref"] for b in self.service.list_batches("viewer")}
        self.assertIn("B-9", priority_batches)
        self.assertNotIn("B-8", priority_batches)
        self.assertEqual(review_batches, {"B-10"})
        self.assertEqual(len(all_batches), 3)
        with self.assertRaises(ValueError):
            self.service.list_batches("viewer", "unknown")
        # 只有inspector能提交批次
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch({"external_ref": "X", "readings": [
                {"section": "s", "metric": "seepage", "value": 1,
                 "control_value": 1}]}, "v", "viewer")
        # 任务只由对应责任角色接手
        task = [t for t in bad["tasks"] if t["assignee_role"] == "dam_engineer"][0]
        with self.assertRaises(PermissionDenied):
            self.service.claim_task(task["id"], "mgr", "emergency_manager")
        claimed = self.service.claim_task(task["id"], "eng", "dam_engineer")
        self.assertEqual(claimed["status"], "claimed")
        with self.assertRaises(ConflictError):
            self.service.claim_task(task["id"], "eng2", "dam_engineer")
        # 无效读数
        with self.assertRaises(ValidationError):
            self._batch("B-11", [{"section": "s", "metric": "tilt",
                                  "value": 1, "control_value": 1}])
        # 批次编号重复
        with self.assertRaises(ConflictError):
            self._batch("B-9", [{"section": "s", "metric": "seepage",
                                 "value": 1, "control_value": 1}])
        del review


if __name__ == "__main__":
    unittest.main()
