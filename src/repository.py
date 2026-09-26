from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (INVALID_REASON_RECORDS_CHANGED, INVALID_REASON_STATUS_CHANGED,
                    ID_PREFIX, STATES)


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
                    updated_at TEXT NOT NULL,
                    closed_signoff_id INTEGER
                        REFERENCES signoffs(id) ON DELETE SET NULL
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
                CREATE TABLE IF NOT EXISTS signoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    basis_version INTEGER NOT NULL,
                    conclusion TEXT NOT NULL,
                    evidence_ref TEXT,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('submitted','approved','rejected','invalidated')),
                    reviewed_by TEXT,
                    review_comment TEXT,
                    reviewed_at TEXT,
                    invalidated_reason TEXT
                        CHECK(invalidated_reason IS NULL
                              OR invalidated_reason IN ('status_changed','records_changed')),
                    invalidated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_signoffs_item
                    ON signoffs(item_id, id);
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
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
            if "closed_signoff_id" not in cols:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN closed_signoff_id INTEGER "
                    "REFERENCES signoffs(id) ON DELETE SET NULL")

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
                        actor: str) -> tuple:
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
            invalidated: List[int] = []
            if target != "closed":
                invalidated = self._invalidate_signoffs(
                    item_id, INVALID_REASON_STATUS_CHANGED, now)
            else:
                # 关闭在同一事务内强校验：必须存在当前版本、未失效的通过会签，
                # 防止关闭瞬间会签被并发退回或数据变动。
                row = self.conn.execute(
                    """SELECT id FROM signoffs
                       WHERE item_id=? AND basis_version=? AND status='approved'
                         AND invalidated_reason IS NULL LIMIT 1""",
                    (item_id, expected_version),
                ).fetchone()
                if row is None:
                    raise ConflictError("缺少当前版本通过的关闭会签，不能关闭事件")
                self.conn.execute(
                    "UPDATE items SET closed_signoff_id=? WHERE id=?",
                    (int(row["id"]), item_id))
        return self.get_item(item_id), invalidated

    def _invalidate_signoffs(self, item_id: int, reason: str,
                             now: Optional[str] = None) -> List[int]:
        """将活动会签置为失效，必须在持有连接事务的调用方内执行。"""
        now = now or utc_now()
        cur = self.conn.execute(
            """UPDATE signoffs SET status='invalidated', invalidated_reason=?,
                   invalidated_at=?, updated_at=?
               WHERE item_id=? AND invalidated_reason IS NULL
                 AND status IN ('submitted','approved','rejected')""",
            (reason, now, now, item_id),
        )
        if cur.rowcount == 0:
            return []
        rows = self.conn.execute(
            """SELECT id FROM signoffs WHERE item_id=? AND status='invalidated'
               AND invalidated_reason=? AND invalidated_at=?""",
            (item_id, reason, now),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> tuple:
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
                # 记录变动后原会签依据的数据已变化，活动会签一律失效需重办；
                # 已关闭事件的补充记录不再影响关闭时采用的会签。
                status = self.conn.execute(
                    "SELECT status FROM items WHERE id=?", (item_id,)).fetchone()["status"]
                invalidated = []
                if status != "closed":
                    invalidated = self._invalidate_signoffs(
                        item_id, INVALID_REASON_RECORDS_CHANGED, now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row), invalidated

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

    def create_signoff(self, item_id: int, basis_version: int, conclusion: str,
                       evidence_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO signoffs(item_id, basis_version, conclusion, evidence_ref,
                   submitted_by, submitted_at, status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,'submitted',?,?)""",
                (item_id, basis_version, conclusion, evidence_ref,
                 actor, now, now, now),
            )
            signoff_id = int(cur.lastrowid)
        return self.get_signoff(signoff_id)

    def get_signoff(self, signoff_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone()
        if row is None:
            raise NotFoundError("会签不存在")
        return dict(row)

    def list_signoffs(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM signoffs WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_active_signoff(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM signoffs WHERE item_id=? AND invalidated_reason IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def review_signoff(self, signoff_id: int, decision: str, comment: Optional[str],
                       reviewer: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone()
            if row is None:
                raise NotFoundError("会签不存在")
            current = dict(row)
            if (current["status"] not in ("submitted", "rejected")
                    or current["invalidated_reason"] is not None):
                raise ConflictError("该会签已处理或已失效，不能再审核")
            self.conn.execute(
                """UPDATE signoffs SET status=?, review_comment=?, reviewed_by=?,
                   reviewed_at=?, updated_at=? WHERE id=?""",
                (decision, comment, reviewer, now, now, signoff_id),
            )
        return self.get_signoff(signoff_id)

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
