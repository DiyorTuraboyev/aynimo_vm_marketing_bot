"""SQLite storage: Telegram photo cache, sent posts and "Bajarildi" confirmations."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_cache (
    photo_path TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    week        TEXT,
    source_file TEXT,
    started_by  TEXT,
    sent        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    cancelled   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    department  TEXT NOT NULL,
    week        TEXT,
    excel_row   INTEGER,
    model       TEXT,
    name        TEXT,
    note        TEXT,
    chat_id     TEXT,
    message_id  INTEGER,
    has_photo   INTEGER DEFAULT 1,
    caption     TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_messages (
    run_id     INTEGER NOT NULL,
    chat_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    post_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    user_name    TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
"""


FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return now().strftime(FMT)


class Storage:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
            if "report_sent_at" not in columns:  # migration for older databases
                self._conn.execute("ALTER TABLE runs ADD COLUMN report_sent_at TEXT")
            post_columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(posts)")}
            if "region" not in post_columns:
                self._conn.execute("ALTER TABLE posts ADD COLUMN region TEXT")
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- photo cache -------------------------------------------------------
    def cached_file_id(self, photo_path: str) -> str | None:
        rows = self._all("SELECT file_id FROM photo_cache WHERE photo_path = ?", (photo_path,))
        return rows[0]["file_id"] if rows else None

    def cache_file_id(self, photo_path: str, file_id: str) -> None:
        self._exec(
            "INSERT INTO photo_cache(photo_path, file_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(photo_path) DO UPDATE SET file_id = excluded.file_id, updated_at = excluded.updated_at",
            (photo_path, file_id, _now()),
        )

    def forget_file_id(self, photo_path: str) -> None:
        self._exec("DELETE FROM photo_cache WHERE photo_path = ?", (photo_path,))

    # ---- runs & posts ------------------------------------------------------
    def start_run(self, week: str, source_file: str, started_by: str) -> int:
        cur = self._exec(
            "INSERT INTO runs(started_at, week, source_file, started_by) VALUES (?, ?, ?, ?)",
            (_now(), week, source_file, started_by),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, sent: int, failed: int, cancelled: bool) -> None:
        self._exec(
            "UPDATE runs SET finished_at = ?, sent = ?, failed = ?, cancelled = ? WHERE id = ?",
            (_now(), sent, failed, int(cancelled), run_id),
        )

    def create_post(self, run_id: int, department: str, week: str, excel_row: int,
                    model: str, name: str, note: str, chat_id, caption: str, region: str = "") -> int:
        cur = self._exec(
            "INSERT INTO posts(run_id, department, week, excel_row, model, name, note, chat_id, caption, "
            "created_at, region) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, department, week, excel_row, model, name, note, str(chat_id), caption, _now(), region),
        )
        return int(cur.lastrowid)

    def set_post_message(self, post_id: int, message_id: int, has_photo: bool, caption: str) -> None:
        self._exec(
            "UPDATE posts SET message_id = ?, has_photo = ?, caption = ? WHERE id = ?",
            (message_id, int(has_photo), caption, post_id),
        )

    def delete_post(self, post_id: int) -> None:
        self._exec("DELETE FROM posts WHERE id = ?", (post_id,))

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    # ---- confirmations -----------------------------------------------------
    def toggle_confirmation(self, post_id: int, user_id: int, user_name: str) -> bool:
        """Returns True if the user is now marked as done, False if unmarked."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM confirmations WHERE post_id = ? AND user_id = ?", (post_id, user_id)
            ).fetchone()
            if exists:
                self._conn.execute(
                    "DELETE FROM confirmations WHERE post_id = ? AND user_id = ?", (post_id, user_id)
                )
            else:
                self._conn.execute(
                    "INSERT INTO confirmations(post_id, user_id, user_name, confirmed_at) VALUES (?, ?, ?, ?)",
                    (post_id, user_id, user_name, _now()),
                )
            self._conn.commit()
            return not exists

    def confirmations(self, post_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT user_name, confirmed_at FROM confirmations WHERE post_id = ? ORDER BY confirmed_at",
            (post_id,),
        )

    # ---- reporting ---------------------------------------------------------
    def last_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def runs_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        """Runs that actually posted something in [start, end)."""
        return self._all(
            "SELECT * FROM runs WHERE started_at >= ? AND started_at < ? AND sent > 0 ORDER BY id",
            (start.strftime(FMT), end.strftime(FMT)),
        )

    def mark_report_sent(self, run_ids: list[int]) -> None:
        if not run_ids:
            return
        marks = ",".join("?" for _ in run_ids)
        self._exec(f"UPDATE runs SET report_sent_at = ? WHERE id IN ({marks})", (_now(), *run_ids))

    # ---- cleaning (admin /clear) ------------------------------------------
    def posts_of_runs(self, run_ids: list[int], only_confirmed: bool = False) -> list[sqlite3.Row]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        extra = " AND id IN (SELECT post_id FROM confirmations)" if only_confirmed else ""
        return self._all(
            f"SELECT * FROM posts WHERE run_id IN ({marks}) AND message_id IS NOT NULL{extra}",
            tuple(run_ids),
        )

    def add_run_message(self, run_id: int, chat_id, message_id: int) -> None:
        """Extra messages of a send (department headers) — so /clear can delete them too."""
        self._exec(
            "INSERT INTO run_messages(run_id, chat_id, message_id) VALUES (?, ?, ?)",
            (run_id, str(chat_id), message_id),
        )

    def run_messages(self, run_ids: list[int]) -> list[sqlite3.Row]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        return self._all(f"SELECT * FROM run_messages WHERE run_id IN ({marks})", tuple(run_ids))

    def confirmation_count(self, run_ids: list[int]) -> int:
        if not run_ids:
            return 0
        marks = ",".join("?" for _ in run_ids)
        rows = self._all(
            f"SELECT COUNT(*) AS n FROM confirmations WHERE post_id IN "
            f"(SELECT id FROM posts WHERE run_id IN ({marks}))",
            tuple(run_ids),
        )
        return int(rows[0]["n"])

    def clear_confirmations(self, run_ids: list[int]) -> int:
        """Removes every 'Bajarildi' mark of these runs. Returns how many were removed."""
        if not run_ids:
            return 0
        marks = ",".join("?" for _ in run_ids)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM confirmations WHERE post_id IN (SELECT id FROM posts WHERE run_id IN ({marks}))",
                tuple(run_ids),
            )
            self._conn.execute(f"UPDATE runs SET report_sent_at = NULL WHERE id IN ({marks})", tuple(run_ids))
            self._conn.commit()
            return cur.rowcount

    def delete_runs(self, run_ids: list[int]) -> None:
        """Forget these sends completely (posts, marks, run records)."""
        if not run_ids:
            return
        marks = ",".join("?" for _ in run_ids)
        with self._lock:
            self._conn.execute(
                f"DELETE FROM confirmations WHERE post_id IN (SELECT id FROM posts WHERE run_id IN ({marks}))",
                tuple(run_ids),
            )
            self._conn.execute(f"DELETE FROM posts WHERE run_id IN ({marks})", tuple(run_ids))
            self._conn.execute(f"DELETE FROM run_messages WHERE run_id IN ({marks})", tuple(run_ids))
            self._conn.execute(f"DELETE FROM runs WHERE id IN ({marks})", tuple(run_ids))
            self._conn.commit()

    def run_chats(self, run_ids: list[int]) -> list[str]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        rows = self._all(f"SELECT DISTINCT chat_id FROM posts WHERE run_id IN ({marks})", tuple(run_ids))
        return [r["chat_id"] for r in rows if r["chat_id"]]

    def confirmation_rows(self, run_ids: list[int]) -> list[sqlite3.Row]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        return self._all(
            f"""
            SELECT p.department, c.user_id, c.user_name, c.confirmed_at
            FROM confirmations c JOIN posts p ON p.id = c.post_id
            WHERE p.run_id IN ({marks})
            """,
            tuple(run_ids),
        )

    def report_rows(self, run_ids: list[int]) -> list[sqlite3.Row]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        return self._all(
            f"""
            SELECT p.run_id, p.week, p.department, p.excel_row, p.model, p.name, p.region, p.note,
                   p.chat_id, p.message_id, p.created_at,
                   COUNT(c.user_id) AS done_count,
                   GROUP_CONCAT(c.user_name || ' (' || c.confirmed_at || ')', '; ') AS done_by
            FROM posts p
            LEFT JOIN confirmations c ON c.post_id = p.id
            WHERE p.run_id IN ({marks}) AND p.message_id IS NOT NULL
            GROUP BY p.id
            ORDER BY p.run_id, p.id
            """,
            tuple(run_ids),
        )
