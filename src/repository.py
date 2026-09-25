from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_ref TEXT,
                    inspector TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_batches_ref
                    ON batches(batch_ref) WHERE batch_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    dam_section TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    control_value REAL NOT NULL,
                    breach INTEGER NOT NULL DEFAULT 0,
                    external_ref TEXT,
                    reported_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK(state IN ('pending_review','confirmed')),
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_readings_batch ON readings(batch_id);
                CREATE INDEX IF NOT EXISTS ix_readings_state ON readings(state);
                CREATE INDEX IF NOT EXISTS ix_readings_ref ON readings(external_ref);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
                    assignee TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','accepted','done')),
                    detail TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    accepted_by TEXT,
                    accepted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---- 巡检批次 / 读数 / 处置任务 ----
    def create_batch(self, batch_ref: Optional[str], inspector: str, note: str,
                     actor: str, created_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO batches(batch_ref, inspector, note, created_by, created_at)
                   VALUES(?,?,?,?,?)""",
                (batch_ref, inspector, note, actor, created_at),
            )
            batch_id = int(cur.lastrowid)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def find_batch_by_ref(self, batch_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE batch_ref=?", (batch_ref,)).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batches ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def create_reading(self, batch_id: int, item_id: Optional[int], dam_section: str,
                       metric: str, value: float, control_value: float, breach: bool,
                       external_ref: Optional[str], reported_at: str, state: str,
                       note: str, actor: str, created_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO readings(batch_id, item_id, dam_section, metric, value,
                   control_value, breach, external_ref, reported_at, state, note,
                   created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, item_id, dam_section, metric, value, control_value,
                 1 if breach else 0, external_ref, reported_at, state, note,
                 actor, created_at),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        reading = dict(row)
        reading["breach"] = bool(reading["breach"])
        return reading

    def list_readings(self, batch_id: Optional[int] = None,
                      state: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM readings"
        clauses, params = [], []
        if batch_id is not None:
            clauses.append("batch_id=?"); params.append(batch_id)
        if state:
            clauses.append("state=?"); params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            reading = dict(row)
            reading["breach"] = bool(reading["breach"])
            result.append(reading)
        return result

    def find_reading_by_ref(self, external_ref: str) -> Optional[Dict[str, Any]]:
        """按外部编号找历史读数，用于重复编号合并与矛盾判定"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM readings WHERE external_ref=?
                   ORDER BY reported_at ASC, id ASC""",
                (external_ref,)).fetchall()
        if not rows:
            return None
        reading = dict(rows[0])
        reading["breach"] = bool(reading["breach"])
        return reading

    def update_reading(self, reading_id: int, **fields) -> Dict[str, Any]:
        if not fields:
            return self.get_reading(reading_id)
        columns = ", ".join(f"{key}=?" for key in fields)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE readings SET {columns} WHERE id=?",
                (*fields.values(), reading_id))
            if cur.rowcount == 0:
                raise NotFoundError("读数不存在")
        return self.get_reading(reading_id)

    def create_task(self, item_id: int, reading_id: Optional[int], assignee: str,
                    due_at: str, detail: str, actor: str,
                    created_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO tasks(item_id, reading_id, assignee, due_at, status,
                   detail, created_by, created_at) VALUES(?,?,?,?,'open',?,?,?)""",
                (item_id, reading_id, assignee, due_at, detail, actor, created_at),
            )
            task_id = int(cur.lastrowid)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("处置任务不存在")
        return dict(row)

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY due_at ASC, id ASC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def open_task_exists(self, item_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE item_id=? AND status!='done'",
                (item_id,)).fetchone()
        return int(row["n"]) > 0

    def accept_task(self, task_id: int, actor: str, accepted_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE tasks SET status='accepted', accepted_by=?, accepted_at=?
                   WHERE id=? AND status='open'""",
                (actor, accepted_at, task_id))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("处置任务不存在")
                raise ConflictError("该任务已被接手")
        return self.get_task(task_id)

    def has_closed_reinspection(self, item_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM records
                   WHERE item_id=? AND kind=? AND status='closed'""",
                (item_id, "reinspection")).fetchone()
        return int(row["n"]) > 0

    def priority_item_ids(self) -> List[int]:
        """优先队列：存在超控制值读数的缺陷ID"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT item_id FROM readings WHERE breach=1 AND item_id IS NOT NULL"
            ).fetchall()
        return [int(row["item_id"]) for row in rows]

    def review_batch_ids(self) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT batch_id FROM readings WHERE state='pending_review'"
            ).fetchall()
        return [int(row["batch_id"]) for row in rows]

    def batch_item_ids(self, batch_id: int) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT item_id FROM readings WHERE batch_id=? AND item_id IS NOT NULL",
                (batch_id,)).fetchall()
        return [int(row["item_id"]) for row in rows]
