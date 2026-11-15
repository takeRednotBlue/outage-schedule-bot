import json
from pathlib import Path
import sqlite3

from .schedule import Schedule


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY, minutes INTEGER NOT NULL DEFAULT 30,
                active INTEGER NOT NULL DEFAULT 1, since REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivered (
                chat_id INTEGER NOT NULL, event TEXT NOT NULL, sent REAL NOT NULL,
                PRIMARY KEY(chat_id, event));
        """)

    def subscribe(self, chat_id: int, now: float):
        with self.db:
            self.db.execute("INSERT INTO subscribers(chat_id, since) VALUES (?, ?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET active=1, since=excluded.since", (chat_id, now))

    def subscribers(self):
        return self.db.execute("SELECT * FROM subscribers WHERE active=1").fetchall()

    def subscriber(self, chat_id):
        return self.db.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()

    def pause(self, chat_id):
        with self.db:
            self.db.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (chat_id,))

    def reminder(self, chat_id, minutes):
        with self.db:
            self.db.execute("UPDATE subscribers SET minutes=? WHERE chat_id=?", (minutes, chat_id))

    def forget(self, chat_id):
        with self.db:
            self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
            self.db.execute("DELETE FROM delivered WHERE chat_id=?", (chat_id,))
            self.db.execute("DELETE FROM state WHERE key=?", (f"change:{chat_id}",))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))

    def load_schedule(self):
        value = self.get("schedule")
        return Schedule.from_json(value) if value is not None else None

    def delivered(self, chat_id, event):
        return self.db.execute("SELECT 1 FROM delivered WHERE chat_id=? AND event=?", (chat_id, event)).fetchone() is not None

    def mark(self, chat_id, event, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO delivered VALUES (?, ?, ?)", (chat_id, event, now))

    def prune(self, now):
        with self.db:
            self.db.execute("DELETE FROM delivered WHERE sent < ?", (now - 7 * 86400,))
