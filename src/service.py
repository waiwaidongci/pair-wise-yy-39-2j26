from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_metric,
                     normalize_severity, normalize_task_state, parse_iso8601,
                     require_number, require_text, validate_queue)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CREATE_ROLES, BATCH_ENTITY, CREATE_ROLES,
                    ENTITY, METRIC_LABELS, READING_ENTITY, RECORD_ROLES,
                    REVIEW_ROLES, TASK_ACCEPT_ROLES, TASK_ENTITY, TERMINAL_STATES,
                    TITLE, VIEW_ROLES, arrival_deadline_hours, breach_severity,
                    close_blockers, default_assignee, escalation_required,
                    exceeds_control, priority_score, reading_contradicts,
                    reinspection_required, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

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
        if target in TERMINAL_STATES:
            blockers = close_blockers(
                self.repository.open_record_count(item_id),
                reinspection_required(item),
                self.repository.has_closed_reinspection(item_id))
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

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["requires_reinspection"] = reinspection_required(item)
        return result

    # ---- 巡检批次接入缺陷流程 ----
    def create_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        inspector = require_text(payload.get("inspector", actor), "inspector", 100)
        note = payload.get("note", "")
        if note is None:
            note = ""
        note = require_text(note, "note", 2000) if str(note).strip() else ""
        batch_ref = payload.get("batch_ref")
        if batch_ref is not None:
            batch_ref = require_text(batch_ref, "batch_ref", 100)
            if self.repository.find_batch_by_ref(batch_ref):
                raise ConflictError("批次外部编号已存在")
        raw_readings = payload.get("readings")
        if not isinstance(raw_readings, list) or not raw_readings:
            raise ConflictError("一批至少携带一个坝段读数")
        if len(raw_readings) > 500:
            raise ConflictError("单批读数不能超过500条")

        prepared: List[Dict[str, Any]] = []
        seen_refs: Dict[str, int] = {}
        for index, raw in enumerate(raw_readings):
            if not isinstance(raw, dict):
                raise ConflictError(f"第{index + 1}条读数格式不合法")
            prefix = f"readings[{index}]"
            dam_section = require_text(raw.get("dam_section"), f"{prefix}.dam_section", 100)
            metric = normalize_metric(raw.get("metric"))
            value = require_number(raw.get("value"), f"{prefix}.value")
            control = require_number(raw.get("control_value"),
                                     f"{prefix}.control_value", 0.000001)
            external_ref = raw.get("external_ref")
            if external_ref is not None:
                external_ref = require_text(external_ref, f"{prefix}.external_ref", 100)
                if external_ref in seen_refs:
                    raise ConflictError(
                        f"批次内external_ref重复：{external_ref}（第{seen_refs[external_ref] + 1}、{index + 1}条）")
                seen_refs[external_ref] = index
            reported_dt = parse_iso8601(
                raw.get("reported_at", utc_now()), f"{prefix}.reported_at")
            reading_note = str(raw.get("note", "") or "")
            if len(reading_note) > 2000:
                raise ConflictError(f"{prefix}.note不能超过2000个字符")
            prepared.append({
                "dam_section": dam_section, "metric": metric, "value": value,
                "control": control, "external_ref": external_ref,
                "reported_at": reported_dt.isoformat(),
                "reported_dt": reported_dt, "note": reading_note,
            })
        # 重复外部编号按最早报告时间合并，必须先处理最早的读数
        prepared.sort(key=lambda r: (r["reported_dt"], r["external_ref"] or ""))

        now = utc_now()
        batch = self.repository.create_batch(batch_ref, inspector, note, actor, now)
        batch_id = batch["id"]
        tasks_created: List[Dict[str, Any]] = []

        for entry in prepared:
            breach = exceeds_control(entry["value"], entry["control"])
            item_id: Optional[int] = None
            state = "confirmed"
            reading_note = entry["note"]
            previous = (self.repository.find_reading_by_ref(entry["external_ref"])
                        if entry["external_ref"] else None)
            new_item = False

            if previous is not None:
                # 重复外部编号：合并到最早报告的缺陷
                item_id = previous["item_id"]
                if reading_contradicts(entry["value"], previous["value"]):
                    state = "pending_review"
                    reading_note = (reading_note + " " if reading_note else "") + \
                        f"与读数#{previous['id']}矛盾，待复核"
                else:
                    reading_note = (reading_note + " " if reading_note else "") + \
                        f"合并至读数#{previous['id']}（同一外部编号）"
            elif breach:
                new_item = True
                severity = breach_severity(entry["value"], entry["control"])
                label = METRIC_LABELS[entry["metric"]]
                title = f"{entry['dam_section']}{label}超控制值"
                description = (
                    f"巡检批次#{batch_id}：{entry['dam_section']} {label}"
                    f"读数{entry['value']:g}，控制值{entry['control']:g}"
                    f"{('；' + entry['note']) if entry['note'] else ''}")
                item = self.repository.create_item(
                    title, description, severity, entry["value"], entry["control"],
                    entry["external_ref"], actor)
                item_id = item["id"]
                self.repository.append_audit("batch_breach", ENTITY, item_id, actor, {
                    "batch_id": batch_id, "dam_section": entry["dam_section"],
                    "metric": entry["metric"], "value": entry["value"],
                    "control_value": entry["control"], "severity": severity,
                    "priority": priority_score(severity, entry["value"], entry["control"]),
                })

            reading = self.repository.create_reading(
                batch_id, item_id, entry["dam_section"], entry["metric"],
                entry["value"], entry["control"], breach, entry["external_ref"],
                entry["reported_at"], state, reading_note, actor, now)

            # 仅新建的超控制值缺陷立即生成带责任人和到场时限的处置任务；
            # 合并读数与待复核读数不生成
            if new_item:
                task = self._create_task_for_reading(
                    item_id, reading["id"], entry, actor, now)
                tasks_created.append(task)

        self.repository.append_audit("batch_create", BATCH_ENTITY, batch_id, actor, {
            "batch_ref": batch_ref, "inspector": inspector,
            "readings": len(prepared), "tasks": len(tasks_created),
        })
        return self.get_batch(batch_id, "inspector")

    def _create_task_for_reading(self, item_id: int, reading_id: int,
                                 entry: Dict[str, Any], actor: str,
                                 now: str) -> Dict[str, Any]:
        severity = breach_severity(entry["value"], entry["control"])
        assignee = default_assignee(entry["metric"])
        hours = arrival_deadline_hours(severity, entry["value"], entry["control"])
        due_at = (datetime.fromisoformat(now) + timedelta(hours=hours)).isoformat()
        detail = (f"{entry['dam_section']} {METRIC_LABELS[entry['metric']]}"
                  f"读数{entry['value']:g}>控制值{entry['control']:g}，"
                  f"需在{hours}小时内到场处置")
        task = self.repository.create_task(
            item_id, reading_id, assignee, due_at, detail, actor, now)
        self.repository.append_audit("task_create", TASK_ENTITY, task["id"], actor, {
            "item_id": item_id, "reading_id": reading_id,
            "assignee": assignee, "due_at": due_at, "severity": severity,
        })
        return task

    def list_batches(self, role: str, queue: Optional[str] = None) -> list:
        self._view(role)
        if queue:
            validate_queue(queue)
        batches = [self.enrich_batch(row) for row in self.repository.list_batches()]
        if queue == "review":
            review_ids = set(self.repository.review_batch_ids())
            batches = [b for b in batches if b["id"] in review_ids]
        elif queue == "priority":
            priority_ids = set(self.repository.priority_item_ids())
            batches = [b for b in batches
                       if any(iid in priority_ids for iid in b["item_ids"])]
        return batches

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_id)
        return self.enrich_batch(batch)

    def enrich_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        readings = self.repository.list_readings(batch["id"])
        result["readings"] = readings
        result["item_ids"] = sorted({r["item_id"] for r in readings
                                     if r["item_id"] is not None})
        result["breach_count"] = sum(1 for r in readings if r["breach"])
        result["pending_review_count"] = sum(
            1 for r in readings if r["state"] == "pending_review")
        return result

    def review_reading(self, reading_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.get_reading(reading_id)
        if reading["state"] != "pending_review":
            raise ConflictError("该读数不在待复核状态")
        action = require_text(payload.get("action"), "action", 20)
        note = str(payload.get("note", "") or "")
        if len(note) > 2000:
            raise ConflictError("note不能超过2000个字符")
        fields: Dict[str, Any] = {"state": "confirmed"}
        if note:
            fields["note"] = (reading["note"] + " " if reading["note"] else "") + \
                f"复核结论（{actor}）：{note}"

        if action == "confirm":
            # 确认异常：无归属缺陷则补建缺陷并生成处置任务
            updated = self.repository.update_reading(reading_id, **fields)
            if reading["item_id"] is None and reading["breach"]:
                severity = breach_severity(reading["value"], reading["control_value"])
                label = METRIC_LABELS[reading["metric"]]
                item = self.repository.create_item(
                    f"{reading['dam_section']}{label}超控制值（复核确认）",
                    f"待复核读数#{reading_id}经{actor}确认："
                    f"读数{reading['value']:g}，控制值{reading['control_value']:g}",
                    severity, reading["value"], reading["control_value"],
                    reading["external_ref"], actor)
                self.repository.update_reading(reading_id, item_id=item["id"])
                entry = {"dam_section": reading["dam_section"],
                         "metric": reading["metric"],
                         "value": reading["value"],
                         "control": reading["control_value"]}
                self._create_task_for_reading(item["id"], reading_id, entry,
                                              actor, utc_now())
                updated = self.repository.get_reading(reading_id)
            elif reading["item_id"] is not None and reading["breach"] \
                    and not self.repository.open_task_exists(reading["item_id"]):
                entry = {"dam_section": reading["dam_section"],
                         "metric": reading["metric"],
                         "value": reading["value"],
                         "control": reading["control_value"]}
                self._create_task_for_reading(
                    reading["item_id"], reading_id, entry, actor, utc_now())
            self.repository.append_audit("reading_confirm", READING_ENTITY,
                                         reading_id, actor, {"action": action})
            return updated
        if action == "dismiss":
            fields["breach"] = 0
            updated = self.repository.update_reading(reading_id, **fields)
            self.repository.append_audit("reading_dismiss", READING_ENTITY,
                                         reading_id, actor, {"action": action})
            return updated
        raise ConflictError("action必须是confirm或dismiss")

    def list_tasks(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status:
            normalize_task_state(status)
        return self.repository.list_tasks(status)

    def get_task(self, task_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_task(task_id)

    def accept_task(self, task_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self.repository.accept_task(task_id, actor, utc_now())
        self.repository.append_audit("task_accept", TASK_ENTITY, task_id, actor, {
            "item_id": task["item_id"], "assignee": task["assignee"],
        })
        return task

    def list_items(self, role: str, status: Optional[str] = None,
                   queue: Optional[str] = None) -> list:
        self._view(role)
        if queue:
            validate_queue(queue)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        if queue == "priority":
            priority_ids = set(self.repository.priority_item_ids())
            items = [item for item in items if item["id"] in priority_ids]
            items.sort(key=lambda item: item["priority"], reverse=True)
        elif queue == "review":
            review_readings = self.repository.list_readings(state="pending_review")
            review_ids = {r["item_id"] for r in review_readings
                          if r["item_id"] is not None}
            items = [item for item in items if item["id"] in review_ids]
        return items
