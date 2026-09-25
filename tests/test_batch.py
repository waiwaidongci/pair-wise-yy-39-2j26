import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
from src.domain import ConflictError, PermissionDenied, ValidationError


def batch_payload():
    return {
        "batch_ref": "B-2026-001", "inspector": "张巡",
        "note": "汛期3号坝段例行巡检",
        "readings": [
            {"dam_section": "3号坝段", "metric": "seepage", "value": 31.0,
             "control_value": 20.0, "external_ref": "RD-3-SEEP"},
            {"dam_section": "3号坝段", "metric": "displacement", "value": 2.0,
             "control_value": 10.0, "external_ref": "RD-3-DISP"},
            {"dam_section": "5号坝段", "metric": "crack", "value": 5.5,
             "control_value": 2.0, "external_ref": "RD-5-CRACK",
             "reported_at": "2026-09-25T08:00:00+00:00"},
        ],
    }


class BatchWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def test_batch_creates_items_priority_queue_and_tasks(self):
        batch = self.service.create_batch(batch_payload(), "张巡", "inspector")
        self.assertEqual(len(batch["readings"]), 3)
        # 两条超控制值（渗流31>20、裂缝5.5>2），一条正常
        self.assertEqual(batch["breach_count"], 2)
        self.assertEqual(len(batch["item_ids"]), 2)

        tasks = self.service.list_tasks("viewer")
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertEqual(task["status"], "open")
            self.assertTrue(task["assignee"])
            self.assertTrue(task["due_at"])

        # 优先队列：只含超控制值缺陷，并按priority降序
        priority = self.service.list_items("viewer", queue="priority")
        self.assertEqual({i["external_ref"] for i in priority},
                         {"RD-3-SEEP", "RD-5-CRACK"})
        self.assertGreaterEqual(priority[0]["priority"], priority[-1]["priority"])
        normal = [i for i in self.service.list_items("viewer")
                  if i["external_ref"] == "RD-3-DISP"]
        self.assertEqual(normal, [])

        # 批次列表可按优先队列筛选
        batches = self.service.list_batches("viewer", queue="priority")
        self.assertEqual([b["id"] for b in batches], [batch["id"]])

        # 应急任务有人接手：dam_engineer 可accept
        task = tasks[0]
        accepted = self.service.accept_task(task["id"], "李工", "dam_engineer")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accepted_by"], "李工")
        with self.assertRaises(ConflictError):
            self.service.accept_task(task["id"], "王工", "emergency_manager")
        with self.assertRaises(PermissionDenied):
            self.service.accept_task(tasks[1]["id"], "路人", "viewer")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_ref_merges_earliest_contradiction_pending_review(self):
        first = self.service.create_batch(batch_payload(), "张巡", "inspector")
        first_item_id = next(r["item_id"] for r in first["readings"]
                             if r["external_ref"] == "RD-3-SEEP")

        # 后到批次重复外部编号：读数一致 -> 合并，不新建缺陷
        merged_payload = {
            "batch_ref": "B-2026-002", "inspector": "张巡",
            "readings": [
                {"dam_section": "3号坝段", "metric": "seepage", "value": 31.2,
                 "control_value": 20.0, "external_ref": "RD-3-SEEP",
                 "reported_at": "2026-09-26T08:00:00+00:00"},
            ],
        }
        merged = self.service.create_batch(merged_payload, "张巡", "inspector")
        merged_reading = merged["readings"][0]
        self.assertEqual(merged_reading["state"], "confirmed")
        self.assertEqual(merged_reading["item_id"], first_item_id)
        items = self.service.list_items("viewer", queue="priority")
        self.assertEqual(len([i for i in items
                              if i["external_ref"] == "RD-3-SEEP"]), 1)

        # 矛盾读数（相对差>10%）进入待复核，不重复生成任务
        conflict_payload = {
            "batch_ref": "B-2026-003", "inspector": "张巡",
            "readings": [
                {"dam_section": "3号坝段", "metric": "seepage", "value": 80.0,
                 "control_value": 20.0, "external_ref": "RD-3-SEEP",
                 "reported_at": "2026-09-27T08:00:00+00:00"},
            ],
        }
        conflict = self.service.create_batch(conflict_payload, "张巡", "inspector")
        pending = conflict["readings"][0]
        self.assertEqual(pending["state"], "pending_review")
        self.assertEqual(pending["item_id"], first_item_id)
        task_count = len(self.service.list_tasks("viewer"))
        self.assertEqual(task_count, 2)

        # 页面可按待复核筛选批次
        review_batches = self.service.list_batches("viewer", queue="review")
        self.assertIn(conflict["id"], {b["id"] for b in review_batches})
        review_items = self.service.list_items("viewer", queue="review")
        self.assertIn(first_item_id, {i["id"] for i in review_items})

        # 复核确认：读数转为confirmed（已归属缺陷，不补建、不重复任务）
        reviewed = self.service.review_reading(
            pending["id"], {"action": "confirm", "note": "现场复测确为80"},
            "李工", "dam_engineer")
        self.assertEqual(reviewed["state"], "confirmed")
        self.assertEqual(len(self.service.list_tasks("viewer")), 2)

    def test_earlier_report_arrives_late_merges_to_earliest(self):
        # 先报告 09-26 的读数
        late = {
            "batch_ref": "B-LATE", "inspector": "张巡",
            "readings": [
                {"dam_section": "7号坝段", "metric": "seepage", "value": 22.0,
                 "control_value": 20.0, "external_ref": "RD-7",
                 "reported_at": "2026-09-26T00:00:00+00:00"},
            ],
        }
        b1 = self.service.create_batch(late, "张巡", "inspector")
        # 后登记但报告时间更早（09-20）：新读数自成缺陷
        early = {
            "batch_ref": "B-EARLY", "inspector": "张巡",
            "readings": [
                {"dam_section": "7号坝段", "metric": "seepage", "value": 21.0,
                 "control_value": 20.0, "external_ref": "RD-7",
                 "reported_at": "2026-09-20T00:00:00+00:00"},
            ],
        }
        b2 = self.service.create_batch(early, "张巡", "inspector")
        earliest_item = b2["readings"][0]["item_id"]
        self.assertIsNotNone(earliest_item)
        # 再来一条同编号，应合并到最早报告时间对应的缺陷
        again = {
            "batch_ref": "B-AGAIN", "inspector": "张巡",
            "readings": [
                {"dam_section": "7号坝段", "metric": "seepage", "value": 21.5,
                 "control_value": 20.0, "external_ref": "RD-7",
                 "reported_at": "2026-09-28T00:00:00+00:00"},
            ],
        }
        b3 = self.service.create_batch(again, "张巡", "inspector")
        self.assertEqual(b3["readings"][0]["item_id"], earliest_item)

    def test_reinspection_missing_blocks_close(self):
        batch = self.service.create_batch(batch_payload(), "张巡", "inspector")
        item_id = batch["item_ids"][0]
        current = self.service.get_item(item_id, "viewer")
        self.assertTrue(current["requires_reinspection"])
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "r",
                TRANSITION_ROLES[target][0])
        # 缺复检记录不能关闭
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                current["id"], "closed", current["version"], "r",
                TRANSITION_ROLES["closed"][0])
        self.assertIn("复检", str(ctx.exception))
        # 复检登记为未关闭同样不能关闭
        open_record = self.service.add_record(
            item_id, {"kind": "reinspection", "detail": "复检中", "status": "open"},
            "张巡", "inspector")
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], "closed", current["version"], "r",
                TRANSITION_ROLES["closed"][0])
        # 复检完成（closed）后方可关闭（未关闭事项也必须清零）
        self.repo.conn.execute(
            "UPDATE records SET status='closed' WHERE id=?", (open_record["id"],))
        closed = self.service.transition(
            current["id"], "closed", current["version"], "r",
            TRANSITION_ROLES["closed"][0])
        self.assertEqual(closed["status"], "closed")

    def test_batch_validation_and_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(batch_payload(), "x", "viewer")
        bad = batch_payload(); bad["readings"] = []
        with self.assertRaises(ConflictError):
            self.service.create_batch(bad, "张巡", "inspector")
        bad_metric = batch_payload()
        bad_metric["batch_ref"] = "B-BAD"
        bad_metric["readings"][0]["metric"] = "ph"
        with self.assertRaises(ValidationError):
            self.service.create_batch(bad_metric, "张巡", "inspector")
        # 批次外部编号重复
        self.service.create_batch(batch_payload(), "张巡", "inspector")
        dup = batch_payload()
        dup["readings"][0]["external_ref"] = "OTHER-1"
        dup["readings"][1]["external_ref"] = "OTHER-2"
        dup["readings"][2]["external_ref"] = "OTHER-3"
        with self.assertRaises(ConflictError):
            self.service.create_batch(dup, "张巡", "inspector")
        # 批次内外部编号重复
        inner = {"batch_ref": "B-INNER", "readings": [
            {"dam_section": "1", "metric": "seepage", "value": 1,
             "control_value": 2, "external_ref": "X"},
            {"dam_section": "2", "metric": "seepage", "value": 1,
             "control_value": 2, "external_ref": "X"},
        ]}
        with self.assertRaises(ConflictError):
            self.service.create_batch(inner, "张巡", "inspector")


if __name__ == "__main__":
    unittest.main()
