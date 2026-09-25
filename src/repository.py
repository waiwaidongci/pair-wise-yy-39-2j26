from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, READING_STATES, STATES


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
        reading_states = ",".join("'" + s + "'" for s in READING_STATES)
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
                    updated_at TEXT NOT NULL,
                    source_batch_id INTEGER,
                    metric TEXT,
                    section TEXT,
                    requires_reinspection INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS inspection_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_ref TEXT NOT NULL UNIQUE,
                    note TEXT,
                    reported_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES inspection_batches(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    external_ref TEXT,
                    section TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    control_value REAL NOT NULL,
                    reported_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ({reading_states})),
                    severity TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    canonical_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
                    repeat_count INTEGER NOT NULL DEFAULT 0,
                    conflict_note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_readings_external_ref ON readings(external_ref);
                CREATE INDEX IF NOT EXISTS ix_readings_batch ON readings(batch_id);
                CREATE TABLE IF NOT EXISTS disposal_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    assignee TEXT NOT NULL,
                    assignee_role TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','claimed')),
                    claimed_by TEXT,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_tasks_item ON disposal_tasks(item_id);
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
            """)
            self._migrate_columns("items", {
                "source_batch_id": "INTEGER",
                "metric": "TEXT",
                "section": "TEXT",
                "requires_reinspection": "INTEGER NOT NULL DEFAULT 0",
            })

    def _migrate_columns(self, table: str, columns: Dict[str, str]) -> None:
        existing = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, initial_status: Optional[str] = None,
                    source_batch_id: Optional[int] = None,
                    metric: Optional[str] = None, section: Optional[str] = None,
                    requires_reinspection: bool = False) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       source_batch_id, metric, section, requires_reinspection)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold,
                     initial_status or STATES[0], 1,
                     external_ref, actor, now, now,
                     source_batch_id, metric, section, 1 if requires_reinspection else 0),
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

    def closed_reinspection_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? "
                "AND status='closed' AND kind='reinspection'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 巡检批次 -------------------------------------------------------
    def create_batch(self, external_ref: str, note: Optional[str], reported_at: str,
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO inspection_batches(external_ref, note, reported_at,
                       created_by, created_at) VALUES(?,?,?,?,?)""",
                    (external_ref, note, reported_at, actor, now),
                )
                batch_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次外部编号已存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM inspection_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("巡检批次不存在")
        return dict(row)

    def list_batches(self, queue: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT DISTINCT b.* FROM inspection_batches b"
        params: tuple = ()
        if queue == "priority":
            sql += " JOIN readings r ON r.batch_id=b.id WHERE r.state='priority'"
        elif queue == "review":
            sql += " JOIN readings r ON r.batch_id=b.id WHERE r.state='review'"
        sql += " ORDER BY b.id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def insert_reading(self, batch_id: int, seq: int, external_ref: Optional[str],
                       section: str, metric: str, value: float, control_value: float,
                       reported_at: str, state: str, severity: Optional[str],
                       priority: int, item_id: Optional[int], canonical_id: Optional[int],
                       repeat_count: int, conflict_note: Optional[str],
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO readings(batch_id, seq, external_ref, section, metric, value,
                   control_value, reported_at, state, severity, priority, item_id,
                   canonical_id, repeat_count, conflict_note, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, seq, external_ref, section, metric, value, control_value,
                 reported_at, state, severity, priority, item_id, canonical_id,
                 repeat_count, conflict_note, actor, now),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def list_readings(self, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM readings"
        params: tuple = ()
        if batch_id is not None:
            sql += " WHERE batch_id=?"
            params = (batch_id,)
        sql += " ORDER BY batch_id, seq"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def find_canonical_reading(self, external_ref: str) -> Optional[Dict[str, Any]]:
        """同一外部编号按最早报告时间取基准读数。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM readings WHERE external_ref=? AND canonical_id IS NULL
                   ORDER BY reported_at ASC, id ASC LIMIT 1""",
                (external_ref,)).fetchone()
        return dict(row) if row else None

    def mark_reading(self, reading_id: int, state: str, item_id: Optional[int] = None,
                     canonical_id: Optional[int] = None, repeat_count: Optional[int] = None,
                     conflict_note: Optional[str] = None) -> None:
        fields = ["state=?"]
        params: list = [state]
        if item_id is not None:
            fields.append("item_id=?"); params.append(item_id)
        if canonical_id is not None:
            fields.append("canonical_id=?"); params.append(canonical_id)
        if repeat_count is not None:
            fields.append("repeat_count=?"); params.append(repeat_count)
        if conflict_note is not None:
            fields.append("conflict_note=?"); params.append(conflict_note)
        params.append(reading_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE readings SET {', '.join(fields)} WHERE id=?", params)

    # ---- 处置任务 -------------------------------------------------------
    def create_task(self, item_id: int, reading_id: Optional[int], title: str,
                    assignee: str, assignee_role: str, deadline: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO disposal_tasks(item_id, reading_id, title, assignee,
                   assignee_role, deadline, status, created_at)
                   VALUES(?,?,?,?,?,?, 'open', ?)""",
                (item_id, reading_id, title, assignee, assignee_role, deadline, now),
            )
            task_id = int(cur.lastrowid)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disposal_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("处置任务不存在")
        return dict(row)

    def claim_task(self, task_id: int, actor: str, claimed_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE disposal_tasks SET status='claimed', claimed_by=?, claimed_at=?
                   WHERE id=? AND status='open'""",
                (actor, claimed_at, task_id))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM disposal_tasks WHERE id=?", (task_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("处置任务不存在")
                raise ConflictError("任务已被接手")
        return self.get_task(task_id)

    def list_tasks(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM disposal_tasks"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY deadline ASC, id ASC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

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
