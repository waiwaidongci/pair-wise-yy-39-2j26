from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, PermissionDenied, ensure_role,
                     normalize_severity, parse_iso, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CREATE_ROLES, BATCH_VIEW_ROLES, CREATE_ROLES,
                    ENTITY, METRIC_UNITS, RECORD_ROLES, TASK_CLAIM_ROLES,
                    TITLE, VIEW_ROLES, arrival_deadline, assignee_role_for,
                    completion_blockers, escalation_required, metric_label,
                    reading_over_limit, reading_priority, reading_severity,
                    readings_conflict, resolve_assignee, response_deadline_hours,
                    priority_score, role_for_transition, task_title, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        # 外部编号合并的“查重-写入”串行化
        self._batch_lock = threading.RLock()

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            requires_reinspection=bool(item.get("requires_reinspection")),
            closed_reinspections=self.repository.closed_reinspection_count(item_id),
        )
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 巡检批次 -------------------------------------------------------
    def submit_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        batch_reported = parse_iso(payload.get("reported_at"), "reported_at", utc_now())
        raw = payload.get("readings")
        if not isinstance(raw, list) or not raw:
            raise ValueError("readings必须是非空数组")
        if len(raw) > 200:
            raise ValueError("单批读数不能超过200条")
        roster = payload.get("assignees")
        if roster is not None and not isinstance(roster, dict):
            raise ValueError("assignees必须是角色到责任人姓名的映射")
        inputs = [self._parse_reading(r, idx, batch_reported)
                  for idx, r in enumerate(raw, start=1)]
        inputs.sort(key=lambda r: (r["reported_at"], r["seq"]))

        with self._batch_lock:
            batch = self.repository.create_batch(external_ref, note, batch_reported, actor)
            batch_id = batch["id"]
            results = []
            for data in inputs:
                results.append(self._process_reading(batch_id, data, actor, roster))
        self.repository.append_audit("batch_submit", "巡检批次", batch_id, actor, {
            "external_ref": external_ref,
            "readings": len(inputs),
            "priority": sum(1 for r in results if r["state"] == "priority"),
            "review": sum(1 for r in results if r["state"] == "review"),
            "merged": sum(1 for r in results if r["state"] == "merged"),
        })
        return self.get_batch(batch_id, role)

    def _parse_reading(self, raw: Any, seq: int,
                       batch_reported: str) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("读数必须是对象")
        ref = raw.get("external_ref")
        if ref is not None:
            ref = require_text(ref, "readings.external_ref", 100)
        section = require_text(raw.get("section"), "section", 100)
        metric = require_text(raw.get("metric"), "metric", 50)
        metric_label(metric)
        value = require_number(raw.get("value"), "value")
        control = require_number(raw.get("control_value"), "control_value", 0.000001)
        reported_at = parse_iso(raw.get("reported_at"), "reported_at", batch_reported)
        return {"seq": seq, "external_ref": ref, "section": section, "metric": metric,
                "value": value, "control_value": control, "reported_at": reported_at}

    def _process_reading(self, batch_id: int, data: Dict[str, Any],
                         actor: str, roster: Optional[dict]) -> Dict[str, Any]:
        over = reading_over_limit(data["value"], data["control_value"])
        classification = {
            "value": data["value"], "control_value": data["control_value"],
            "over_limit": over,
        }
        # 同一外部编号：按最早报告时间合并，矛盾的后到读数进待复核
        canonical = (self.repository.find_canonical_reading(data["external_ref"])
                     if data["external_ref"] else None)
        if canonical is not None:
            return self._merge_reading(batch_id, data, classification, canonical, actor)

        if over:
            return self._open_priority_reading(batch_id, data, actor, roster)
        reading = self.repository.insert_reading(
            batch_id, data["seq"], data["external_ref"], data["section"],
            data["metric"], data["value"], data["control_value"],
            data["reported_at"], "normal", None, 0, None, None, 0, None, actor)
        return reading

    def _open_priority_reading(self, batch_id: int, data: Dict[str, Any],
                               actor: str, roster: Optional[dict]) -> Dict[str, Any]:
        severity = reading_severity(data["value"], data["control_value"])
        priority = reading_priority(data["value"], data["control_value"])
        label = metric_label(data["metric"])
        unit = METRIC_UNITS[data["metric"]]
        title = task_title(data["metric"], data["section"])
        description = (f"巡检批次#{batch_id} {label}读数 {data['value']:g}{unit}"
                       f"超过控制值 {data['control_value']:g}{unit}，进入优先队列")
        item = self.repository.create_item(
            title, description, severity, data["value"], data["control_value"],
            data["external_ref"], actor, initial_status="inspected",
            source_batch_id=batch_id, metric=data["metric"],
            section=data["section"], requires_reinspection=True)
        reading = self.repository.insert_reading(
            batch_id, data["seq"], data["external_ref"], data["section"],
            data["metric"], data["value"], data["control_value"],
            data["reported_at"], "priority", severity, priority, item["id"],
            None, 1, None, actor)
        deadline = arrival_deadline(data["reported_at"], severity,
                                    data["value"], data["control_value"])
        role_name = assignee_role_for(severity)
        assignee = resolve_assignee(role_name, roster)
        task = self.repository.create_task(item["id"], reading["id"], title,
                                           assignee, role_name, deadline)
        self.repository.append_audit("defect_open", ENTITY, item["id"], actor, {
            "batch_id": batch_id, "reading_id": reading["id"],
            "metric": data["metric"], "section": data["section"],
            "severity": severity, "priority": priority,
            "task_id": task["id"], "assignee": assignee,
            "assignee_role": role_name, "deadline": deadline,
        })
        return reading

    def _merge_reading(self, batch_id: int, data: Dict[str, Any],
                       classification: Dict[str, Any], canonical: Dict[str, Any],
                       actor: str) -> Dict[str, Any]:
        first = {"value": canonical["value"],
                 "control_value": canonical["control_value"],
                 "over_limit": canonical["state"] in ("priority", "merged")}
        conflict = readings_conflict(first, classification)
        if conflict:
            note = (f"与最早报告读数(读数#{canonical['id']}, "
                    f"{canonical['value']:g}/{canonical['control_value']:g})矛盾，待复核")
            reading = self.repository.insert_reading(
                batch_id, data["seq"], data["external_ref"], data["section"],
                data["metric"], data["value"], data["control_value"],
                data["reported_at"], "review", canonical["severity"],
                canonical["priority"], canonical["item_id"], canonical["id"], 0,
                note, actor)
            self.repository.append_audit("reading_review", "巡检读数",
                                         reading["id"], actor, {
                    "external_ref": data["external_ref"],
                    "canonical_reading_id": canonical["id"],
                    "canonical_item_id": canonical["item_id"],
                    "value": data["value"],
                    "control_value": data["control_value"],
                })
            return reading

        # 一致读数合并到最早报告：连续异常在同一缺陷累计
        promoted = data["reported_at"] < canonical["reported_at"]
        repeats = int(canonical["repeat_count"] or 0) + 1
        if promoted:
            # 后到但报告时间更早：新读数提升为基准，原基准降级为合并
            reading = self.repository.insert_reading(
                batch_id, data["seq"], data["external_ref"], data["section"],
                data["metric"], data["value"], data["control_value"],
                data["reported_at"], canonical["state"], canonical["severity"],
                canonical["priority"], canonical["item_id"], None,
                repeats, None, actor)
            self.repository.mark_reading(
                canonical["id"], "merged",
                item_id=canonical["item_id"], canonical_id=reading["id"])
        else:
            reading = self.repository.insert_reading(
                batch_id, data["seq"], data["external_ref"], data["section"],
                data["metric"], data["value"], data["control_value"],
                data["reported_at"], "merged", canonical["severity"],
                canonical["priority"], canonical["item_id"], canonical["id"], 0,
                None, actor)
            self.repository.mark_reading(canonical["id"], canonical["state"],
                                         repeat_count=repeats)
        if canonical["item_id"] is not None and classification["over_limit"]:
            self.repository.add_record(
                canonical["item_id"], "follow_up",
                f"外部编号{data['external_ref']}一致读数重复报告，"
                f"累计第{repeats + 1}次异常",
                "closed", f"RR-{data['external_ref']}-{reading['id']}", actor)
        self.repository.append_audit("reading_merged", "巡检读数",
                                     reading["id"], actor, {
                "external_ref": data["external_ref"],
                "canonical_reading_id": reading["id"] if promoted else canonical["id"],
                "canonical_item_id": canonical["item_id"],
                "repeat_count": repeats + 1, "promoted": promoted,
            })
        return reading

    def list_batches(self, role: str, queue: Optional[str] = None) -> list:
        ensure_role(role, BATCH_VIEW_ROLES)
        if queue is not None and queue not in ("priority", "review"):
            raise ValueError("queue必须是priority或review")
        return [self._enrich_batch(b, role) for b in self.repository.list_batches(queue)]

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_VIEW_ROLES)
        return self._enrich_batch(self.repository.get_batch(batch_id), role)

    def claim_task(self, task_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self.repository.get_task(task_id)
        if task["assignee_role"] != role:
            raise PermissionDenied("该任务由其他角色负责")
        claimed = self.repository.claim_task(task_id, actor, utc_now())
        self.repository.append_audit("task_claim", "处置任务", task_id, actor, {
            "item_id": claimed["item_id"], "assignee": claimed["assignee"],
        })
        return claimed

    def list_tasks(self, role: str, item_id: Optional[int] = None) -> list:
        self._view(role)
        now = utc_now()
        tasks = []
        for task in self.repository.list_tasks(item_id):
            task["overdue"] = task["status"] == "open" and task["deadline"] < now
            tasks.append(task)
        return tasks

    def _enrich_batch(self, batch: Dict[str, Any], role: str) -> Dict[str, Any]:
        result = dict(batch)
        readings = [r for r in self.repository.list_readings(batch["id"])]
        result["readings"] = readings
        result["counts"] = {
            "total": len(readings),
            "priority": sum(1 for r in readings if r["state"] == "priority"),
            "review": sum(1 for r in readings if r["state"] == "review"),
            "merged": sum(1 for r in readings if r["state"] == "merged"),
            "normal": sum(1 for r in readings if r["state"] == "normal"),
        }
        item_ids = {r["item_id"] for r in readings if r["item_id"] is not None}
        result["in_priority_queue"] = result["counts"]["priority"] > 0
        result["needs_review"] = result["counts"]["review"] > 0
        tasks = []
        for task in self.repository.list_tasks():
            if task["item_id"] in item_ids:
                task = dict(task)
                task["overdue"] = (task["status"] == "open"
                                   and task["deadline"] < utc_now())
                tasks.append(task)
        result["tasks"] = tasks
        del role
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
