"""SQLite storage. Dates and ids are plain columns; all content is encrypted."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .crypto import Box

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    raw_text BLOB NOT NULL,
    summary BLOB,
    provider TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    error TEXT,
    wanted_provider TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);

CREATE TABLE IF NOT EXISTS day_summaries (
    entry_date TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    summary BLOB,
    fingerprint TEXT,
    entry_count INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    style TEXT NOT NULL,
    title BLOB NOT NULL,
    content BLOB NOT NULL,
    provider TEXT,
    model TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, box: Box):
        self.path = path
        self.box = box
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Columns added after the first release
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
            for column, ddl in (("status", "TEXT NOT NULL DEFAULT 'done'"), ("error", "TEXT"),
                                ("wanted_provider", "TEXT")):
                if column not in existing:
                    conn.execute(f"ALTER TABLE entries ADD COLUMN {column} {ddl}")
        path.chmod(0o600)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- settings ------------------------------------------------------

    def get_setting(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(self.box.open(row["value"])) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, self.box.seal(json.dumps(value))),
            )

    # ---- entries -------------------------------------------------------

    def _entry(self, row) -> dict:
        return {
            "id": row["id"],
            "date": row["entry_date"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "text": self.box.open(row["raw_text"]),
            "summary": json.loads(self.box.open(row["summary"])) if row["summary"] else None,
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "error": row["error"],
            "wanted_provider": row["wanted_provider"],
        }

    def add_entry(self, entry_date: str, text: str, summary: dict | None, provider: str | None, model: str | None,
                  status: str = "done", wanted_provider: str | None = None) -> dict:
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO entries (entry_date, created_at, updated_at, raw_text, summary, provider, model, "
                "status, wanted_provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_date, now, now, self.box.seal(text),
                 self.box.seal(json.dumps(summary)) if summary else None, provider, model, status, wanted_provider),
            )
            entry_id = cur.lastrowid
        return self.get_entry(entry_id)

    def update_entry(self, entry_id: int, *, entry_date: str | None = None, text: str | None = None,
                     summary: dict | None = None, provider: str | None = None, model: str | None = None) -> dict | None:
        current = self.get_entry(entry_id)
        if not current:
            return None
        new_summary = summary if summary is not None else current["summary"]
        with self._conn() as conn:
            conn.execute(
                "UPDATE entries SET entry_date=?, raw_text=?, summary=?, provider=?, model=?, updated_at=? WHERE id=?",
                (
                    entry_date or current["date"],
                    self.box.seal(text if text is not None else current["text"]),
                    self.box.seal(json.dumps(new_summary)) if new_summary else None,
                    provider or current["provider"],
                    model or current["model"],
                    _now(),
                    entry_id,
                ),
            )
        return self.get_entry(entry_id)

    def set_status(self, entry_id: int, status: str, error: str | None = None,
                   wanted_provider: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE entries SET status=?, error=?, wanted_provider=COALESCE(?, wanted_provider) WHERE id=?",
                (status, error, wanted_provider, entry_id))

    def entry_ids_with_status(self, status: str) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM entries WHERE status=? ORDER BY id", (status,)).fetchall()
        return [r["id"] for r in rows]

    def pending_entry_ids(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM entries WHERE status IN ('transcribing', 'summarising') "
                                "ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def get_entry(self, entry_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._entry(row) if row else None

    def delete_entry(self, entry_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM entries WHERE id=?", (entry_id,)).rowcount > 0

    def list_entries(self, start: str | None = None, end: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM entries WHERE 1=1"
        args: list = []
        if start:
            sql += " AND entry_date >= ?"
            args.append(start)
        if end:
            sql += " AND entry_date <= ?"
            args.append(end)
        sql += " ORDER BY entry_date DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._entry(r) for r in rows]

    # ---- end-of-day summaries -------------------------------------------

    def _day(self, row) -> dict:
        return {
            "date": row["entry_date"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "error": row["error"],
            "summary": json.loads(self.box.open(row["summary"])) if row["summary"] else None,
            "fingerprint": row["fingerprint"],
            "entry_count": row["entry_count"],
            "provider": row["provider"],
            "model": row["model"],
        }

    def get_day(self, entry_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM day_summaries WHERE entry_date=?", (entry_date,)).fetchone()
        return self._day(row) if row else None

    def list_days(self, start: str | None = None, end: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM day_summaries WHERE 1=1", []
        if start:
            sql += " AND entry_date >= ?"
            args.append(start)
        if end:
            sql += " AND entry_date <= ?"
            args.append(end)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY entry_date DESC", args).fetchall()
        return [self._day(r) for r in rows]

    def save_day(self, entry_date: str, *, status: str, error: str | None = None, summary: dict | None = None,
                 fingerprint: str | None = None, entry_count: int | None = None, provider: str | None = None,
                 model: str | None = None) -> None:
        """Upsert; fields left as None keep their stored value (so a refresh keeps the old summary)."""
        current = self.get_day(entry_date) or {}
        values = {
            "summary": summary if summary is not None else current.get("summary"),
            "fingerprint": fingerprint if fingerprint is not None else current.get("fingerprint"),
            "entry_count": entry_count if entry_count is not None else current.get("entry_count", 0),
            "provider": provider or current.get("provider"),
            "model": model or current.get("model"),
        }
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO day_summaries (entry_date, updated_at, status, error, summary, fingerprint, entry_count, "
                "provider, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_date) DO UPDATE SET "
                "updated_at=excluded.updated_at, status=excluded.status, error=excluded.error, "
                "summary=excluded.summary, fingerprint=excluded.fingerprint, entry_count=excluded.entry_count, "
                "provider=excluded.provider, model=excluded.model",
                (entry_date, _now(), status, error,
                 self.box.seal(json.dumps(values["summary"])) if values["summary"] else None,
                 values["fingerprint"], values["entry_count"], values["provider"], values["model"]),
            )

    def delete_day(self, entry_date: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM day_summaries WHERE entry_date=?", (entry_date,))

    def pending_days(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT entry_date FROM day_summaries WHERE status='summarising'").fetchall()
        return [r["entry_date"] for r in rows]

    # ---- reports -------------------------------------------------------

    def _report(self, row, with_content: bool = True) -> dict:
        report = {
            "id": row["id"],
            "created_at": row["created_at"],
            "start": row["start_date"],
            "end": row["end_date"],
            "style": row["style"],
            "title": self.box.open(row["title"]),
            "provider": row["provider"],
            "model": row["model"],
        }
        if with_content:
            report["content"] = self.box.open(row["content"])
        return report

    def add_report(self, start: str, end: str, style: str, title: str, content: str, provider: str, model: str) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reports (created_at, start_date, end_date, style, title, content, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), start, end, style, self.box.seal(title), self.box.seal(content), provider, model),
            )
            report_id = cur.lastrowid
        return self.get_report(report_id)

    def get_report(self, report_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return self._report(row) if row else None

    def list_reports(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
        return [self._report(r, with_content=False) for r in rows]

    def delete_report(self, report_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM reports WHERE id=?", (report_id,)).rowcount > 0
