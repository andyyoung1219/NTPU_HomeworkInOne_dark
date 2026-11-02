"""Small SQLite metadata store with user-scoped keys and atomic transactions."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    user_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,kind,id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    user_id TEXT NOT NULL, task_id TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL, code TEXT, created_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, user, kind, identifier, payload):
        with self.connection() as db:
            self._save(db, user, kind, identifier, payload)

    @staticmethod
    def _save(db, user, kind, identifier, payload):
        db.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(user_id,kind,id) "
                   "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                   (user, kind, identifier, json.dumps(payload, ensure_ascii=False), now()))

    def save_many(self, user, records):
        with self.connection() as db:
            for kind, identifier, payload in records:
                self._save(db, user, kind, identifier, payload)

    def get(self, user, kind, identifier):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM records WHERE user_id=? AND kind=? AND id=?",
                             (user, kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, user, kind):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM records WHERE user_id=? AND kind=? ORDER BY updated_at DESC",
                              (user, kind)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def event(self, user, task_id, event, code=None):
        with self.connection() as db:
            db.execute("INSERT INTO events(user_id,task_id,event,code,created_at) VALUES(?,?,?,?,?)",
                       (user, task_id, event, code, now()))

    def events(self, user, task_id, after=0):
        with self.connection() as db:
            rows = db.execute("SELECT seq,event,code,created_at FROM events WHERE user_id=? AND task_id=? AND seq>? ORDER BY seq",
                              (user, task_id, after)).fetchall()
        return [dict(row) for row in rows]
